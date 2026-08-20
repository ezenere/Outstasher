"""Merge manual de série: leitura das pastas, ordem detectada, pareamento e
criação do job. Os arquivos são de VERDADE (mkv curtos do ffmpeg) — é o
tamanho/duração real que a tela usa para denunciar par errado."""
import asyncio
import subprocess

import pytest

from services import jobs
from services.series import manual


def _mkv(path, dur=1):
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", f"testsrc2=s=64x36:d={dur}:r=5",
         "-f", "lavfi", "-i", f"anoisesrc=d={dur}",
         "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-preset",
         "ultrafast", "-c:a", "ac3", str(path)], check=True)
    return path


def _touch(path):
    """Arquivo de vídeo FALSO: serve para o que não depende de ffprobe."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    return path


def test_scan_agrupa_por_temporada_do_nome_e_da_pasta(tmp_path):
    _touch(tmp_path / "Serie" / "Season 01" / "Serie.S01E01.mkv")
    _touch(tmp_path / "Serie" / "Season 01" / "Serie.S01E02.mkv")
    # sem SxxEyy no nome: a temporada sai da PASTA
    _touch(tmp_path / "Serie" / "Season 02" / "episodio um.mkv")
    _touch(tmp_path / "Serie" / "Season 02" / "episodio dois.mkv")
    _touch(tmp_path / "Serie" / "extras" / "sample.mkv")   # sample é ignorado

    side = manual.scan_side(str(tmp_path / "Serie"), probe=False)
    assert set(side["seasons"]) == {"1", "2"}
    s1, s2 = side["seasons"]["1"], side["seasons"]["2"]
    assert [f["name"] for f in s1["files"]] == ["Serie.S01E01.mkv", "Serie.S01E02.mkv"]
    assert s1["order"].startswith("SxxEyy (E01–E02")
    assert s1["episodes"] == 2
    # temporada 2 não tem numeração no nome: ordem alfabética
    assert s2["order"].startswith("alfabética")
    assert [f["episodes"] for f in s2["files"]] == [[], []]


def test_scan_marca_arquivo_fundido_e_ordem_absoluta(tmp_path):
    _touch(tmp_path / "A" / "Serie.S03E12-E13.mkv")
    _touch(tmp_path / "A" / "Serie.S03E14.mkv")
    a = manual.scan_side(str(tmp_path / "A"), probe=False)["seasons"]["3"]
    assert a["files"][0]["episodes"] == [12, 13]
    assert "fundido" in a["order"]
    assert a["episodes"] == 3          # 12, 13 e 14

    _touch(tmp_path / "B" / "Anime - 137 - Titulo.mkv")
    _touch(tmp_path / "B" / "Anime - 138 - Outro.mkv")
    b = manual.scan_side(str(tmp_path / "B"), probe=False)["seasons"]["unknown"]
    assert b["order"] == "absoluta (137–138)"


def _tmdb(season, n):
    return {season: [{"episode": e, "name": f"Ep {e}"} for e in range(1, n + 1)]}


def test_propose_casa_por_sxxeyy_e_por_posicao(tmp_path):
    for e in (1, 2, 3):
        _touch(tmp_path / "orig" / f"Show.S01E0{e}.mkv")
    # o dublado NÃO numera: casa por posição, na ordem natural
    for nome in ("a primeiro.mkv", "b segundo.mkv", "c terceiro.mkv"):
        _touch(tmp_path / "dub" / nome)
    o = manual.scan_side(str(tmp_path / "orig"), probe=False)
    d = manual.scan_side(str(tmp_path / "dub"), probe=False)
    # o lado dublado ficou em "unknown": a UI atribui a temporada
    d["seasons"]["1"] = d["seasons"].pop("unknown")

    plano = manual.propose(o, d, _tmdb(1, 3))
    assert len(plano) == 1
    linhas = plano[0]["rows"]
    assert [ln["episode"] for ln in linhas] == [1, 2, 3]
    assert [ln["original"].split("/")[-1] for ln in linhas] == [
        "Show.S01E01.mkv", "Show.S01E02.mkv", "Show.S01E03.mkv"]
    assert [ln["dubbed"].split("/")[-1] for ln in linhas] == [
        "a primeiro.mkv", "b segundo.mkv", "c terceiro.mkv"]
    assert all(ln["include"] for ln in linhas)
    assert plano[0]["original"]["files"] == 3
    assert plano[0]["dubbed"]["order"].startswith("alfabética")


def test_propose_arquivo_fundido_entra_nas_duas_linhas(tmp_path):
    _touch(tmp_path / "orig" / "Show.S01E01-E02.mkv")
    _touch(tmp_path / "dub" / "Show.S01E01.mkv")
    _touch(tmp_path / "dub" / "Show.S01E02.mkv")
    o = manual.scan_side(str(tmp_path / "orig"), probe=False)
    d = manual.scan_side(str(tmp_path / "dub"), probe=False)
    linhas = manual.propose(o, d, _tmdb(1, 2))[0]["rows"]
    assert linhas[0]["original"] == linhas[1]["original"]   # o MESMO arquivo
    assert linhas[0]["dubbed"] != linhas[1]["dubbed"]


def test_propose_lista_o_que_sobrou_de_cada_lado(tmp_path):
    _touch(tmp_path / "orig" / "Show.S01E01.mkv")
    _touch(tmp_path / "orig" / "Show.S01E02.mkv")
    _touch(tmp_path / "dub" / "Show.S01E01.mkv")          # falta o E02 dublado
    o = manual.scan_side(str(tmp_path / "orig"), probe=False)
    d = manual.scan_side(str(tmp_path / "dub"), probe=False)
    plano = manual.propose(o, d, _tmdb(1, 2))[0]
    assert plano["rows"][1]["dubbed"] is None
    assert plano["rows"][1]["include"] is False            # não entra sozinho
    assert plano["unmatched"]["original"] == []


@pytest.mark.ffmpeg
def test_scan_le_duracao_de_verdade(tmp_path):
    """A duração é o que denuncia arquivo fundido e par trocado — vem do
    ffprobe, não do nome."""
    _mkv(tmp_path / "s" / "Show.S01E01.mkv", dur=2)
    _mkv(tmp_path / "s" / "Show.S01E02.mkv", dur=1)
    files = manual.scan_side(str(tmp_path / "s"))["seasons"]["1"]["files"]
    assert [round(f["duration"]) for f in files] == [2, 1]
    assert all(f["size"] > 0 for f in files)


def _rows(tmp_path, n=2):
    linhas = []
    for e in range(1, n + 1):
        o = _touch(tmp_path / "o" / f"S01E0{e}.mkv")
        d = _touch(tmp_path / "d" / f"S01E0{e}.mkv")
        linhas.append({"season": 1, "episode": e, "original": str(o),
                       "dubbed": str(d), "include": True})
    return linhas


def test_valida_linhas_antes_de_criar_o_job(tmp_path):
    linhas = _rows(tmp_path)
    assert len(manual._validate_rows(linhas)) == 2

    # excluído não conta
    linhas[1]["include"] = False
    assert len(manual._validate_rows(linhas)) == 1

    with pytest.raises(ValueError, match="Nenhum episódio"):
        manual._validate_rows([])
    with pytest.raises(ValueError, match="falta o arquivo dublado"):
        manual._validate_rows([{"season": 1, "episode": 1,
                                "original": linhas[0]["original"]}])
    with pytest.raises(ValueError, match="não existe"):
        manual._validate_rows([{"season": 1, "episode": 1,
                                "original": linhas[0]["original"],
                                "dubbed": "/nao/existe.mkv"}])
    dup = _rows(tmp_path)
    with pytest.raises(ValueError, match="duas vezes"):
        manual._validate_rows(dup + [dup[0]])


def test_cria_job_pronto_para_o_merge(temp_db, monkeypatch, tmp_path):
    """O job manual tem a MESMA forma dos jobs de série: episódios com
    subestado (já em `downloaded`), sem torrents, direto no merge."""
    temp_db.add_destination("Séries", str(tmp_path / "out"), True, media="tv")
    linhas = _rows(tmp_path, n=2)

    async def fake_details(tmdb_id, lang):
        return {"id": tmdb_id, "original_title": "Serie Exemplo",
                "localized_title": "Serie Exemplo", "english_title": None,
                "original_language": "en", "year": "2010", "overview": None,
                "poster": None}

    async def fake_season(tmdb_id, season):
        return {"episodes": [{"episode": 1, "name": "Piloto", "air_date": None,
                              "runtime": 22},
                             {"episode": 2, "name": "Segundo", "air_date": None,
                              "runtime": 22}]}
    from services import tmdb as tmdb_api
    monkeypatch.setattr(tmdb_api, "tv_details", fake_details)
    monkeypatch.setattr(tmdb_api, "tv_season", fake_season)

    convertidos = []

    async def fake_merge_all(job):
        convertidos.append(sorted(job["episodes"]))
        jobs._set(job, "done", "ok")
    from services.series import merge_runner
    monkeypatch.setattr(merge_runner, "merge_all", fake_merge_all)

    async def go():
        out = await manual.create(42, "pt", linhas)
        jid = out["id"]
        job = jobs._jobs[jid]
        assert job["media_type"] == "tv" and job["mode"] == "files"
        assert job["torrents"] == []
        assert sorted(job["episodes"]) == ["S01E01", "S01E02"]
        ep = job["episodes"]["S01E01"]
        assert ep["state"] == "downloaded" and ep["name"] == "Piloto"
        assert ep["src"] == {"original": linhas[0]["original"],
                             "dubbed": linhas[0]["dubbed"]}
        await jobs._tasks[jid]
        assert convertidos == [["S01E01", "S01E02"]]
    asyncio.run(go())


def test_seasons_found_une_os_dois_lados_e_ignora_unknown(tmp_path):
    _touch(tmp_path / "o" / "Show.S01E01.mkv")
    _touch(tmp_path / "o" / "Show.S02E01.mkv")
    _touch(tmp_path / "o" / "sem numero.mkv")          # vai para "unknown"
    _touch(tmp_path / "d" / "Show.S02E01.mkv")
    _touch(tmp_path / "d" / "Show.S03E01.mkv")
    o = manual.scan_side(str(tmp_path / "o"), probe=False)
    d = manual.scan_side(str(tmp_path / "d"), probe=False)
    assert manual.seasons_found(o, d) == [1, 2, 3]
    assert manual.seasons_found(o, d, wanted=[2, 9]) == [2]


def test_raiz_com_duas_series_usa_a_pasta_dominante(tmp_path):
    """Apontar a raiz para a pasta de torrents inteira mistura séries: o
    episódio 1 não pode vir de outra série só porque o nome dela vem antes no
    alfabeto. Manda a pasta que mais cobre a temporada."""
    outra = tmp_path / "raiz" / "AAA Outra Serie"      # vem primeiro no alfabeto
    certa = tmp_path / "raiz" / "ZZZ Serie Certa"
    _touch(outra / "Outra.S01E01.mkv")                 # só um episódio
    for e in (1, 2, 3):
        _touch(certa / f"Certa.S01E0{e}.mkv")

    lado = manual.scan_side(str(tmp_path / "raiz"), probe=False)
    s1 = lado["seasons"]["1"]
    assert s1["dir"] == str(certa)      # a que cobre mais episódios
    assert s1["dirs"] == 2              # a UI avisa que a raiz mistura pastas

    linhas = manual.propose(lado, lado, _tmdb(1, 3))[0]["rows"]
    assert [ln["original"].split("/")[-2] for ln in linhas] == ["ZZZ Serie Certa"] * 3


def test_mesmo_episodio_em_duas_qualidades_fica_com_o_maior(tmp_path):
    d = tmp_path / "s"
    d.mkdir(parents=True)
    (d / "Show.S01E01.720p.mkv").write_bytes(b"x" * 10)
    (d / "Show.S01E01.1080p.mkv").write_bytes(b"x" * 100)
    lado = manual.scan_side(str(d), probe=False)
    linhas = manual.propose(lado, lado, _tmdb(1, 1))[0]["rows"]
    assert linhas[0]["original"].endswith("1080p.mkv")


def test_pasta_escolhida_para_a_temporada_manda(tmp_path):
    """Escolher a pasta de uma temporada é uma DECISÃO: mesmo que os arquivos
    não digam a temporada (ou digam outra), eles passam a valer para ela."""
    # arquivos sem nenhuma referência de temporada
    for nome in ("cap 1.mkv", "cap 2.mkv", "cap 3.mkv"):
        _touch(tmp_path / "solta" / nome)
    out = manual.scan_season(4, _tmdb(4, 3)[4], [str(tmp_path / "solta")],
                             [str(tmp_path / "solta")])
    linhas = out["season"]["rows"]
    assert [ln["episode"] for ln in linhas] == [1, 2, 3]
    assert [ln["original"].split("/")[-1] for ln in linhas] == [
        "cap 1.mkv", "cap 2.mkv", "cap 3.mkv"]
    assert all(ln["include"] for ln in linhas)
    # os arquivos vão para os dropdowns dos dois lados
    assert len(out["files"]["original"]) == 3


def test_trocar_a_pasta_de_um_lado_nao_apaga_o_outro(tmp_path):
    """Bug de campo: escolher a pasta do áudio zerava o vídeo da temporada.
    Os dois lados vão sempre na chamada — o que não mudou vai como está."""
    _touch(tmp_path / "o" / "Show.S01E01.mkv")
    _touch(tmp_path / "o" / "Show.S01E02.mkv")
    _touch(tmp_path / "novo_dub" / "dub 1.mkv")
    _touch(tmp_path / "novo_dub" / "dub 2.mkv")

    out = manual.scan_season(1, _tmdb(1, 2)[1], [str(tmp_path / "o")],
                             [str(tmp_path / "novo_dub")])
    linhas = out["season"]["rows"]
    assert [ln["original"].split("/")[-1] for ln in linhas] == [
        "Show.S01E01.mkv", "Show.S01E02.mkv"]        # continua lá
    assert [ln["dubbed"].split("/")[-1] for ln in linhas] == [
        "dub 1.mkv", "dub 2.mkv"]                    # veio da pasta nova
    assert all(ln["include"] for ln in linhas)


def test_varias_pastas_no_mesmo_lado_se_somam(tmp_path):
    """Release espalhado: uma pasta por temporada, apontadas juntas."""
    _touch(tmp_path / "t1" / "Show.S01E01.mkv")
    _touch(tmp_path / "t1" / "Show.S01E02.mkv")
    _touch(tmp_path / "t2" / "Show.S02E01.mkv")
    lado = manual.scan_sides([str(tmp_path / "t1"), str(tmp_path / "t2")],
                             probe=False)
    assert sorted(lado["seasons"]) == ["1", "2"]
    assert len(lado["seasons"]["1"]["files"]) == 2
    assert lado["roots"] == [str(tmp_path / "t1"), str(tmp_path / "t2")]
    # a mesma pasta duas vezes não duplica arquivo
    dobrada = manual.scan_sides([str(tmp_path / "t1"), str(tmp_path / "t1")],
                                probe=False)
    assert len(dobrada["seasons"]["1"]["files"]) == 2
