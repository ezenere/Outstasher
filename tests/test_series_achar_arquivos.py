"""Achar o arquivo de cada episódio depois do download, e o que mexe nisso.

Casos reais (job de série grande, 85 episódios em "arquivo não encontrado"
com os arquivos no disco):
- pack de UMA temporada numerado só pela posição ("1 - Nome.mp4");
- temporada com zero a mais no nome ("S012E05");
- torrent movido de pasta no qBittorrent depois de concluído (o job
  procurava no caminho gravado na conclusão);
- capa JPEG embutida derrubando o re-encode dos cortes (SVT-AV1 > 240 fps).
E da tela: tamanho real do torrent no lugar do estimado pelo indexador, e
trocar/remover torrent com o aviso de lacunas aberto no meio do download.
"""
import asyncio

import pytest

from services import jobs
from services.series import parse, pipeline
from services.series.align import render
from tests.test_series_edit_plan import (  # noqa: F401 — fixture
    JOB, FakeQbit, _durante_o_download, baixando)

VIDEO = jobs.VIDEO_EXTENSIONS


# -------------------- nomes --------------------

def test_temporada_com_zero_a_mais():
    assert parse.parse_episode_refs("Serie Exemplo S012E05 - Nome 720p.mp4") == [(12, 5)]
    # as formas de sempre seguem iguais
    assert parse.parse_episode_refs("Serie.Exemplo.S01E02.mkv") == [(1, 2)]
    assert parse.parse_episode_refs("Serie.Exemplo.S10E01.mkv") == [(10, 1)]
    assert parse.parse_episode_refs("Serie.Exemplo.S00E03.mkv") == [(0, 3)]


def test_posicao_em_pack_de_temporada_unica():
    nomes = ["Pasta/1 - Na Hora Certa.mp4", "Pasta/10 - Caçado.mp4",
             "Pasta/2. Outro Nome.mp4", "Pasta/Capa Bluray.jpg",
             "Pasta/10 - Caçado.srt"]
    refs = parse.position_refs(nomes, 2, VIDEO)
    assert refs == {"Pasta/1 - Na Hora Certa.mp4": (2, 1),
                    "Pasta/10 - Caçado.mp4": (2, 10),
                    "Pasta/2. Outro Nome.mp4": (2, 2),
                    "Pasta/10 - Caçado.srt": (2, 10)}


@pytest.mark.parametrize("nomes", [
    # algum arquivo já tem SxxEyy: o pack não é numerado por posição
    ["1 - Nome.mp4", "Serie.S02E02.mp4"],
    # vídeo sem número no início
    ["1 - Nome.mp4", "Extra Bastidores.mp4"],
    # número repetido: ambíguo
    ["1 - Nome.mp4", "1 - Nome (versão longa).mp4"],
    # resolução no início não é episódio
    ["1080p Nome.mp4", "720p Nome.mp4"],
])
def test_posicao_so_sem_ambiguidade(nomes):
    assert parse.position_refs(nomes, 2, VIDEO) == {}


def _pack(tmp_path, nomes):
    d = tmp_path / "Serie Exemplo 2 Temporada"
    d.mkdir()
    for n in nomes:
        (d / n).write_bytes(b"x")
    return d


def test_achar_episodio_pela_posicao(tmp_path):
    d = _pack(tmp_path, ["1 - Primeiro.mp4", "2 - Segundo.mp4", "Capa.jpg"])
    assert pipeline._find_episode_file(d, 2, 2, position_season=2).name == "2 - Segundo.mp4"
    # sem a temporada única (pack de várias temporadas): não adivinha
    with pytest.raises(RuntimeError, match="S02E02"):
        pipeline._find_episode_file(d, 2, 2)
    # temporada pedida diferente da do pack: também não
    with pytest.raises(RuntimeError):
        pipeline._find_episode_file(d, 3, 2, position_season=2)


def test_temporada_unica_vem_do_nome_do_torrent():
    assert pipeline._single_season({"title": "Serie Exemplo 2 Temporada - Dublado"}) == 2
    assert pipeline._single_season({"title": "Serie Exemplo Season 3 720p"}) == 3
    assert pipeline._single_season({"title": "Serie Exemplo S01-S05 720p"}) is None
    assert pipeline._single_season({"title": "Serie Exemplo S02E01 720p"}) is None


def test_selecao_do_pack_usa_a_posicao():
    """A seleção de arquivos (metadados) usa a mesma regra: os episódios do
    pack numerado por posição contam como presentes — sem isso a cobertura
    encolhia para nada e o pack inteiro virava lacuna."""
    job = {"episodes": {f"S02E0{e}": {"season": 2, "episode": e} for e in (1, 2)},
           "order_map": None}
    t = {"title": "Serie Exemplo 2 Temporada", "coverage": ["S02E01", "S02E02"]}
    files = [{"index": 0, "name": "P/1 - Um.mp4"}, {"index": 1, "name": "P/2 - Dois.mp4"},
             {"index": 2, "name": "P/Capa.jpg"}]
    keep, drop, found = pipeline._match_pack_files(job, t, files)
    assert [f["index"] for f in keep] == [0, 1]
    assert [f["index"] for f in drop] == [2]
    assert found == {"S02E01", "S02E02"}


# -------------------- caminho e tamanho atuais --------------------

class QbitComHash(FakeQbit):
    def __init__(self, infos):
        super().__init__()
        self.infos = infos

    async def info_by_hashes(self, hashes):
        return [i for i in self.infos if i["hash"] in hashes]


def test_caminho_e_tamanho_atuais_pelo_hash(monkeypatch):
    qbit = QbitComHash([{"hash": "a" * 40, "content_path": "/ext-data/Pack",
                         "total_size": 6_000_000_000}])
    monkeypatch.setattr(jobs, "_qbit", qbit)
    job = {"id": "x", "events": [], "torrents": [
        {"n": 0, "state": "done", "hash": "a" * 40, "content_path": "/data/Pack",
         "size": 512 * 2**20},
        {"n": 1, "state": "downloading", "hash": "b" * 40, "content_path": None,
         "size": None}]}
    monkeypatch.setattr(jobs, "_event", lambda j, k, m: j["events"].append(m))
    asyncio.run(pipeline._refresh_done_paths(job))
    t0 = job["torrents"][0]
    assert t0["content_path"] == "/ext-data/Pack"
    assert t0["size"] == 6_000_000_000
    assert any("mudou de lugar" in m for m in job["events"])


def test_sem_qbittorrent_fica_o_caminho_gravado(monkeypatch):
    class Fora(FakeQbit):
        async def info_by_hashes(self, hashes):
            raise ConnectionError("fora")
    monkeypatch.setattr(jobs, "_qbit", Fora())
    monkeypatch.setattr(jobs, "_event", lambda j, k, m: None)
    job = {"torrents": [{"n": 0, "state": "done", "hash": "a" * 40,
                         "content_path": "/data/Pack", "size": 1}]}
    asyncio.run(pipeline._refresh_done_paths(job))
    assert job["torrents"][0]["content_path"] == "/data/Pack"


def test_tamanho_dos_metadados():
    t = {"size": 512 * 2**20}
    pipeline._update_size(t, {"total_size": -1})       # metadados ainda não chegaram
    assert t["size"] == 512 * 2**20
    pipeline._update_size(t, {"total_size": 7_340_032_000})
    assert t["size"] == 7_340_032_000


# -------------------- capa no re-encode --------------------

def test_reencode_dos_cortes_ignora_a_capa():
    probe = {"streams": [
        {"index": 0, "codec_type": "video", "disposition": {"attached_pic": 0}},
        {"index": 1, "codec_type": "audio", "disposition": {}},
        {"index": 2, "codec_type": "subtitle"},
        {"index": 3, "codec_type": "video", "disposition": {"attached_pic": 1}},
    ]}
    assert render._map_sem_capa(probe) == ["-map", "0", "-map", "-0:3"]
    assert render._map_sem_capa({"streams": probe["streams"][:3]}) == ["-map", "0"]


# -------------------- aviso de lacunas no meio do download --------------------

def test_trocar_e_remover_com_o_aviso_de_lacunas_aberto(baixando):  # noqa: F811
    job, qbit = baixando
    # remover o dublado abre o aviso de lacunas (watchdog parado)
    _durante_o_download(job, "remove_torrent", {"torrent_n": 1})
    assert job["status"] == "awaiting" and job["awaiting"]["reason"] == "gaps_confirm"
    assert pipeline._paused_at_gaps(job)

    # trocar passa da checagem de estado (aqui não há reserva para o original)
    with pytest.raises(ValueError, match="Sem candidato reserva"):
        asyncio.run(pipeline.switch_torrent(JOB, 0))

    # remover é aplicado na hora — não há watchdog para atender o pedido
    asyncio.run(pipeline.resolve(JOB, "remove_torrent", {"torrent_n": 0}))
    assert job["torrents"] == []
    assert ("a" * 40, True) in qbit.deleted
    assert job["status"] == "awaiting" and job["awaiting"]["reason"] == "gaps_confirm"
