"""Entrega na pasta que JÁ existe no destino, em vez de criar uma paralela.

Caso real (nomes trocados): a série baixada por outro programa estava em
"Serie Exemplo/Season 1/Serie Exemplo - S01E01 - Nome WEBDL-1080p.mkv"; o
catálogo já a reconhecia, mas a entrega montava o caminho só pelo TMDB e
criava "Serie Exemplo (2024) [tmdbid-N]/Season 01/" ao lado — a série
ficava em duas pastas. O mesmo com filme: "Filme Exemplo (2001)/" (sem o id,
de uma versão antiga ou de outro programa) ganhava a vizinha
"Filme Exemplo (2001) [tmdbid-N]/".
"""
import asyncio
from pathlib import Path

import pytest

from services import catalog, jobs, merger
from services.series import merge_runner, naming


# -------------------- helpers --------------------

def _files(base: Path, *rels: str, data: bytes = b"x") -> Path:
    for rel in rels:
        f = base / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(data)
    return base


def _dirs(root: Path) -> list[str]:
    return sorted(p.name for p in root.iterdir() if p.is_dir())


def _events(db, job_id: str) -> list[str]:
    return [e["message"] for e in db.load_events(job_id)]


FILME = {"id": 3131, "original_title": "Filme Exemplo",
         "localized_title": "O Filme de Exemplo", "english_title": None,
         "original_language": "en", "year": "2001", "overview": "", "poster": None}
SERIE = {"id": 4242, "original_title": "Serie Exemplo",
         "localized_title": "A Série de Exemplo", "english_title": None,
         "original_language": "en", "year": "2024", "overview": "", "poster": None}


def _job_serie(dest: Path, episodes, jid: str = "pastaex1") -> dict:
    """Job "só original": cada episódio é entregue por hardlink, sem ffmpeg —
    exercita o cálculo do caminho de saída de ponta a ponta."""
    job = {
        "id": jid, "media_type": "tv", "tmdb_id": 4242, "language": "pt",
        "mode": "auto", "kind": "original", "status": "downloading",
        "detail": "", "convert": None, "movie": dict(SERIE),
        "request": {"seasons": [1], "episodes": {}}, "episodes": {},
        "torrents": [], "awaiting": None, "order_map": None, "progress": {},
        "output": None, "search_queries": [], "search_candidates": [],
        "destination_path": str(dest), "created_at": "2026-11-08T00:00:00",
    }
    src = dest.parent / f"torrent-{jid}"
    src.mkdir(parents=True, exist_ok=True)
    for s, e in episodes:
        f = src / f"Serie.Exemplo.S{s:02d}E{e:02d}.mkv"
        f.write_bytes(b"episodio novo")
        job["episodes"][f"S{s:02d}E{e:02d}"] = {
            "season": s, "episode": e, "name": None, "air_date": "2024-01-01",
            "runtime": 40, "state": "downloaded", "src": {"original": str(f)},
            "output": None, "error": None}
    jobs._jobs[jid] = job
    return job


def _entregar_serie(job: dict) -> dict:
    asyncio.run(merge_runner.merge_all(job))
    assert job["status"] == "done", job["detail"]
    return {k: Path(v["output"]) for k, v in job["episodes"].items()}


def _job_filme(dest: Path, jid: str = "filmeex1", kind: str = "original") -> dict:
    job = {
        "id": jid, "tmdb_id": 3131, "language": "pt", "mode": "auto",
        "kind": kind, "convert": None, "status": "downloading", "detail": "",
        "movie": dict(FILME), "output": None,
        "progress": {"video": None, "audio": None},
        "destination_id": 1, "destination_label": "Filmes",
        "destination_path": str(dest), "created_at": "2026-11-08T00:00:00",
        "search_queries": [], "search_candidates": [],
    }
    jobs._jobs[jid] = job
    return job


@pytest.fixture
def sem_limpeza(monkeypatch):
    """A limpeza dos torrents (qBittorrent) não é o que se testa aqui."""
    async def nada(job):
        return None
    monkeypatch.setattr(jobs.delivery, "_cleanup_torrents", nada)
    monkeypatch.setattr(merge_runner, "_cleanup_torrents", nada)


# -------------------- escolha da pasta (catalog.existing_folder) --------------------

def _achar(root, **kw):
    return catalog.existing_folder(root, "Serie Exemplo", "2024", 4242,
                                   ("A Série de Exemplo",), **kw)


def test_sem_pasta_existente(tmp_path):
    assert _achar(tmp_path / "nao-existe") is None
    (tmp_path / "Outra Serie").mkdir()
    (tmp_path / ".Serie Exemplo").mkdir()          # oculta não conta
    assert _achar(tmp_path) is None


def test_pasta_de_outro_programa_casa_pelo_titulo(tmp_path):
    (tmp_path / "Serie Exemplo").mkdir()
    got = _achar(tmp_path)
    assert (got["folder"], got["match"], got["canonical"]) == \
        ("Serie Exemplo", "title", False)
    assert got["others"] == []
    # pelo localizado também (pasta criada à mão em português)
    (tmp_path / "Serie Exemplo").rmdir()
    (tmp_path / "A Série de Exemplo").mkdir()
    assert _achar(tmp_path)["folder"] == "A Série de Exemplo"


def test_pasta_com_outro_id_nunca_casa(tmp_path):
    (tmp_path / "Serie Exemplo [tmdbid-777]").mkdir()   # reboot homônimo
    (tmp_path / "Serie Exemplo (1990)").mkdir()         # ano de outra série
    assert _achar(tmp_path) is None


def test_ordem_de_preferencia(tmp_path):
    """canônica > mesmo id > nome padrão sem id > título exato > frouxo."""
    nomes = ["S3R1E Exemplo", "Serie Exemplo", "Serie Exemplo (2024)",
             "Outro Nome [tmdbid-4242]", "Serie Exemplo (2024) [tmdbid-4242]"]
    esperado = [("Serie Exemplo (2024) [tmdbid-4242]", "nome padrão"),
                ("Outro Nome [tmdbid-4242]", "marcada com o id do TMDB"),
                ("Serie Exemplo (2024)", "nome padrão, sem o id do TMDB"),
                ("Serie Exemplo", "casou pelo título"),
                ("S3R1E Exemplo", "casou pelo título, com grafia aproximada")]
    for n in nomes:
        (tmp_path / n).mkdir()
    for pasta, why in esperado:
        got = _achar(tmp_path)
        assert (got["folder"], got["why"]) == (pasta, why)
        assert got["canonical"] == (pasta == esperado[0][0])
        assert sorted(got["others"]) == sorted(
            n for n in nomes if n != pasta and (tmp_path / n).exists())
        (tmp_path / pasta).rmdir()
    assert _achar(tmp_path) is None


def test_ambiguidade_real_usa_o_nome_mais_proximo(tmp_path):
    """Duas pastas alheias que casam do mesmo jeito: ganha a de nome mais
    parecido com o título, e o porquê vai para o evento."""
    (tmp_path / "Serie - Exemplo").mkdir()
    (tmp_path / "Serie Exemplo").mkdir()
    got = _achar(tmp_path)
    assert got["folder"] == "Serie Exemplo"
    assert "2 pastas casam do mesmo jeito" in got["why"]
    assert got["others"] == ["Serie - Exemplo"]


def test_filme_exige_o_ano(tmp_path):
    """Filme: a pasta sem ano pode ser o original de um remake homônimo."""
    (tmp_path / "Filme Exemplo").mkdir()
    assert catalog.existing_folder(tmp_path, "Filme Exemplo", "2001", 3131,
                                   require_year=True) is None
    # série aceita (é o normal dos outros programas)
    assert catalog.existing_folder(tmp_path, "Filme Exemplo", "2001", 3131)
    # marcada com o id dispensa o ano
    (tmp_path / "Exemplo Film [tmdbid-3131]").mkdir()
    assert catalog.existing_folder(tmp_path, "Filme Exemplo", "2001", 3131,
                                   require_year=True)["folder"] == \
        "Exemplo Film [tmdbid-3131]"


# -------------------- subpasta da temporada --------------------

@pytest.mark.parametrize("existentes, temporada, esperado", [
    (["Season 1"], 1, "Season 1"),               # reaproveita, sem criar "Season 01"
    (["Season 1"], 2, "Season 2"),               # nova, no padrão da pasta
    (["Season 1", "Season 2"], 12, "Season 12"),
    (["Season 01"], 3, "Season 03"),
    (["season 1"], 2, "season 2"),               # caixa da pasta
    (["Season 10"], 2, "Season 02"),             # "10" não diz nada do zero
    (["Season 1", "Season 01"], 1, "Season 01"),  # as duas: a do padrão
    (["Season 1", "Specials"], 0, "Specials"),
    (["Season 1"], 0, "Season 0"),
    ([], 1, "Season 01"),                        # sem subpasta: padrão do Outstasher
    (["Extras"], 1, "Season 01"),
])
def test_temporada_no_padrao_da_pasta(tmp_path, existentes, temporada, esperado):
    for n in existentes:
        (tmp_path / n).mkdir()
    assert naming.season_dir_in(tmp_path, temporada) == esperado


# -------------------- séries: entrega de ponta a ponta --------------------

def test_episodio_novo_entra_na_pasta_de_outro_programa(temp_db, tmp_path, sem_limpeza):
    """O caso real: temporada existente reaproveitada, temporada nova criada
    no padrão da pasta, nenhuma pasta paralela, evento uma vez só."""
    dest = tmp_path / "tv"
    alheio = "Season 1/Serie Exemplo - S01E01 - Nome WEBDL-1080p.mkv"
    _files(dest / "Serie Exemplo", alheio, data=b"do outro programa")
    job = _job_serie(dest, [(1, 2), (1, 3), (2, 1)])

    out = _entregar_serie(job)

    pasta = dest / "Serie Exemplo"
    assert _dirs(dest) == ["Serie Exemplo"]               # nada ao lado
    assert out["S01E02"] == pasta / "Season 1" / "Serie Exemplo (2024) S01E02 [orig].mkv"
    assert out["S01E03"].parent == pasta / "Season 1"
    assert out["S02E01"] == pasta / "Season 2" / "Serie Exemplo (2024) S02E01 [orig].mkv"
    assert _dirs(pasta) == ["Season 1", "Season 2"]
    assert all(p.is_file() for p in out.values())
    assert (pasta / alheio).read_bytes() == b"do outro programa"
    notas = [m for m in _events(temp_db, job["id"]) if m.startswith("📁")]
    assert notas == ["📁 Entrega na pasta existente 'Serie Exemplo' (casou pelo "
                     "título), em vez de criar 'Serie Exemplo (2024) [tmdbid-4242]'"]


def test_pasta_padronizada_mantem_a_temporada_dela(temp_db, tmp_path, sem_limpeza):
    """Depois do "Padronizar nome" a pasta é a canônica, mas a temporada
    continua "Season 1": o episódio novo vai nela, sem evento (nada a contar)."""
    dest = tmp_path / "tv"
    pasta = dest / "Serie Exemplo (2024) [tmdbid-4242]"
    _files(pasta, "Season 1/Serie Exemplo - S01E01 - Nome WEBDL-1080p.mkv")
    job = _job_serie(dest, [(1, 2)])
    out = _entregar_serie(job)
    assert out["S01E02"].parent == pasta / "Season 1"
    assert _dirs(pasta) == ["Season 1"]
    assert not [m for m in _events(temp_db, job["id"]) if m.startswith("📁")]


def test_pasta_com_tmdbid_diferente_e_ignorada(temp_db, tmp_path, sem_limpeza):
    dest = tmp_path / "tv"
    _files(dest / "Serie Exemplo [tmdbid-777]", "Season 1/Serie Exemplo - S01E01.mkv")
    job = _job_serie(dest, [(1, 2)])
    out = _entregar_serie(job)
    assert out["S01E02"] == (dest / "Serie Exemplo (2024) [tmdbid-4242]" / "Season 01"
                             / "Serie Exemplo (2024) S01E02 [orig].mkv")
    assert list((dest / "Serie Exemplo [tmdbid-777]" / "Season 1").iterdir()) == \
        [dest / "Serie Exemplo [tmdbid-777]" / "Season 1" / "Serie Exemplo - S01E01.mkv"]


def test_duas_candidatas_prefere_a_padronizada(temp_db, tmp_path, sem_limpeza):
    """A padronizada e a alheia no mesmo destino: vai na padronizada, e o
    evento avisa que a série está espalhada."""
    dest = tmp_path / "tv"
    canonica = dest / "Serie Exemplo (2024) [tmdbid-4242]"
    _files(canonica, "Season 01/Serie Exemplo (2024) S01E01 [orig].mkv")
    _files(dest / "Serie Exemplo", "Season 1/Serie Exemplo - S01E05.mkv")
    job = _job_serie(dest, [(1, 2), (1, 3)])
    out = _entregar_serie(job)
    assert {p.parent for p in out.values()} == {canonica / "Season 01"}
    assert sorted(p.name for p in (dest / "Serie Exemplo" / "Season 1").iterdir()) == \
        ["Serie Exemplo - S01E05.mkv"]
    notas = [m for m in _events(temp_db, job["id"]) if m.startswith("📁")]
    assert notas == ["📁 Entrega na pasta 'Serie Exemplo (2024) [tmdbid-4242]' "
                     "(nome padrão) — também casa(m) neste destino: 'Serie Exemplo'"]


def test_duas_alheias_usa_a_mais_proxima_e_registra(temp_db, tmp_path, sem_limpeza):
    dest = tmp_path / "tv"
    _files(dest / "Serie - Exemplo", "Season 1/a S01E01.mkv")
    _files(dest / "Serie Exemplo", "Season 1/b S01E01.mkv")
    job = _job_serie(dest, [(1, 2)])
    out = _entregar_serie(job)
    assert out["S01E02"].parent == dest / "Serie Exemplo" / "Season 1"
    nota = next(m for m in _events(temp_db, job["id"]) if m.startswith("📁"))
    assert "'Serie Exemplo'" in nota and "nome mais próximo" in nota
    assert "também casa(m) neste destino: 'Serie - Exemplo'" in nota


def test_pasta_so_em_outro_destino_nao_e_usada(temp_db, tmp_path, sem_limpeza):
    """A pasta existe noutro destino de séries: a entrega fica no destino do
    job (escolha do usuário — o modal já pré-seleciona o da pasta)."""
    outro = tmp_path / "tv2"
    _files(outro / "Serie Exemplo", "Season 1/Serie Exemplo - S01E01.mkv")
    dest = tmp_path / "tv1"
    dest.mkdir()
    job = _job_serie(dest, [(1, 2)])
    out = _entregar_serie(job)
    assert out["S01E02"].is_relative_to(dest / "Serie Exemplo (2024) [tmdbid-4242]")
    assert _dirs(outro / "Serie Exemplo") == ["Season 1"]
    assert len(list((outro / "Serie Exemplo" / "Season 1").iterdir())) == 1


def test_episodio_nao_sobrescreve_arquivo_alheio(temp_db, tmp_path, sem_limpeza):
    """Na pasta de outro programa, um arquivo que já tem o nome que o
    Outstasher daria não é deste job: a entrega grava ao lado, com (2)."""
    dest = tmp_path / "tv"
    nome = "Season 1/Serie Exemplo (2024) S01E02 [orig].mkv"
    _files(dest / "Serie Exemplo", nome, data=b"do usuario")
    job = _job_serie(dest, [(1, 2)])
    out = _entregar_serie(job)
    assert out["S01E02"].name == "Serie Exemplo (2024) S01E02 [orig] (2).mkv"
    assert out["S01E02"].read_bytes() == b"episodio novo"
    assert (dest / "Serie Exemplo" / nome).read_bytes() == b"do usuario"
    assert any("já existe 'Serie Exemplo (2024) S01E02 [orig].mkv'" in m
               for m in _events(temp_db, job["id"]))


def test_saida_do_proprio_job_e_dele(tmp_path):
    """Re-render do mesmo episódio pelo mesmo job: o arquivo é dele."""
    from services.jobs import runtime
    alvo = tmp_path / "Serie Exemplo" / "Season 1" / "x.mkv"
    _files(tmp_path, "Serie Exemplo/Season 1/x.mkv")
    alheia = {"folder": "Serie Exemplo", "canonical": False}
    assert runtime._unclaimed(alvo, alheia, str(alvo)) == alvo
    assert runtime._unclaimed(alvo, alheia, None).name == "x (2).mkv"
    # pasta padrão: substituir é o "refazer" de sempre
    assert runtime._unclaimed(alvo, {"canonical": True}, None) == alvo
    assert runtime._unclaimed(alvo, None, None) == alvo


# -------------------- filmes --------------------

def test_filme_entra_na_pasta_existente(temp_db, tmp_path, sem_limpeza):
    dest = tmp_path / "filmes"
    _files(dest / "Filme Exemplo (2001)", "Filme Exemplo (2001) Bluray-1080p.mkv",
           data=b"de outro programa")
    src = tmp_path / "dl" / "Filme.Exemplo.2001.mkv"
    _files(src.parent, src.name, data=b"filme novo")
    job = _job_filme(dest)

    asyncio.run(jobs._deliver_single(job, {"video": src}))

    assert job["status"] == "done", job["detail"]
    assert _dirs(dest) == ["Filme Exemplo (2001)"]
    out = dest / "Filme Exemplo (2001)" / "Filme Exemplo (2001) [orig].mkv"
    assert job["output"] == str(out) and out.read_bytes() == b"filme novo"
    assert (dest / "Filme Exemplo (2001)" / "Filme Exemplo (2001) Bluray-1080p.mkv"
            ).read_bytes() == b"de outro programa"
    assert ("📁 Entrega na pasta existente 'Filme Exemplo (2001)' (nome padrão, "
            "sem o id do TMDB), em vez de criar 'Filme Exemplo (2001) "
            "[tmdbid-3131]'") in _events(temp_db, job["id"])


def test_filme_nao_sobrescreve_arquivo_de_mesmo_nome(temp_db, tmp_path, sem_limpeza):
    """'Filme Exemplo (2001)/' de uma versão antiga já tem o arquivo com o
    nome que o Outstasher daria: nada sobrescrito, a entrega vai com (2)."""
    dest = tmp_path / "filmes"
    antigo = dest / "Filme Exemplo (2001)" / "Filme Exemplo (2001) [orig].mkv"
    _files(antigo.parent, antigo.name, data=b"versao antiga")
    src = tmp_path / "dl" / "Filme.Exemplo.2001.mkv"
    _files(src.parent, src.name, data=b"filme novo")
    job = _job_filme(dest)

    asyncio.run(jobs._deliver_single(job, {"video": src}))

    assert job["output"] == str(antigo.with_name("Filme Exemplo (2001) [orig] (2).mkv"))
    assert Path(job["output"]).read_bytes() == b"filme novo"
    assert antigo.read_bytes() == b"versao antiga"


def test_filme_pasta_que_nao_e_dele_e_ignorada(temp_db, tmp_path, sem_limpeza):
    """Sem ano (pode ser o original de um remake), outro ano ou outro id:
    a entrega cria a pasta canônica, como antes."""
    dest = tmp_path / "filmes"
    for n in ("Filme Exemplo", "Filme Exemplo (1980)", "Filme Exemplo (2001) [tmdbid-777]"):
        (dest / n).mkdir(parents=True)
    src = tmp_path / "dl" / "Filme.Exemplo.2001.mkv"
    _files(src.parent, src.name)
    job = _job_filme(dest)
    asyncio.run(jobs._deliver_single(job, {"video": src}))
    assert Path(job["output"]).parent == dest / "Filme Exemplo (2001) [tmdbid-3131]"
    assert not any(m.startswith("📁") for m in _events(temp_db, job["id"]))


def test_filme_merge_usa_a_pasta_existente(temp_db, tmp_path, sem_limpeza, monkeypatch):
    """O merge (dois lados) e o render avançado montam a saída pelo mesmo
    _movie_output — a pasta existente vale para eles também."""
    dest = tmp_path / "filmes"
    (dest / "Filme Exemplo (2001)").mkdir(parents=True)
    dl = tmp_path / "dl"
    _files(dl, "v.mkv", "a.mkv")
    pedido = {}

    def falso_merge(f1, f2, output, *a, **kw):
        pedido["output"] = output
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        Path(output).write_bytes(b"merge")
        r = merger.MergeResult(output=output)
        r.linked, r.offset_ms = False, 0.0
        r.input_shifts, r.input_rates = (0.0, 0.0), (1.0, 1.0)
        return r

    monkeypatch.setattr(merger, "merge", falso_merge)
    job = _job_filme(dest, kind="both")
    asyncio.run(jobs._merge(job, dl / "v.mkv", dl / "a.mkv"))
    assert job["status"] == "done", job["detail"]
    assert pedido["output"] == str(dest / "Filme Exemplo (2001)"
                                   / "Filme Exemplo (2001) [pt+orig].mkv")
    assert _dirs(dest) == ["Filme Exemplo (2001)"]
