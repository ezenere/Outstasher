"""O Outstasher enxergando o que já existe: séries baixadas por OUTROS
programas no destino (pasta sem ano nem [tmdbid-N], "Season 1", nomes
"Serie - S01E01 - Titulo WEBDL-1080p.mkv"), o selo da busca vindo do destino
de séries, o "na coleção" do modal casando pelo título original quando a UI só
conhece o localizado, e os episódios em jobs ativos ("baixando agora").

Caso real (nomes trocados): a pasta "Serie Exemplo/Season 1/" com 8 episódios
em duas qualidades não aparecia em lugar nenhum — o selo da busca olhava só os
destinos de FILMES, e o modal mandava o título localizado ("A Série de
Exemplo"), que não casa com a pasta no original.
"""
from pathlib import Path

import pytest

from services import catalog, jobs
from services.series import activity


# -------------------- helpers --------------------

@pytest.fixture
def db(temp_db):
    """Banco isolado SEM o destino semeado do OUTPUT_DIR (que pode apontar
    para uma pasta real): cada teste cadastra só os seus."""
    for d in temp_db.list_destinations():
        temp_db.delete_destination(d["id"])
    return temp_db


def _dest(db, root: Path, media: str = "tv", label: str = "Séries") -> int:
    root.mkdir(parents=True, exist_ok=True)
    return db.add_destination(label, str(root), False, media)["id"]


def _files(base: Path, *rels: str) -> Path:
    for rel in rels:
        f = base / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"x")
    return base


def _serie_de_outro_programa(root: Path, folder: str = "Serie Exemplo") -> Path:
    """Layout de outro gerenciador: sem ano/id, 'Season 1' sem zero, e dois
    arquivos (qualidades diferentes) para alguns episódios."""
    rels = [f"Season 1/{folder} - S01E{e:02d} - Titulo {e} WEBDL-1080p.mkv"
            for e in range(1, 9)]
    rels += [f"Season 1/{folder} - S01E0{e} - Titulo {e} HDTV-720p.mkv"
             for e in (1, 2)]
    rels += ["Season 1/" + f"{folder} - S01E01 - Titulo 1 WEBDL-1080p.pt.srt"]
    return _files(root / folder, *rels)


def _client(monkeypatch, details=None, search=None):
    from fastapi.testclient import TestClient
    import main
    monkeypatch.setattr(main.auth, "is_password_set", lambda: True)
    monkeypatch.setattr(main.auth, "validate_token", lambda tok: True)
    monkeypatch.setattr(main.config, "TMDB_API_KEY", "chave-de-teste")

    async def tv_details(tv_id, language=None):
        if details is None:
            raise RuntimeError("TMDB fora do ar")
        return details

    async def search_tv(q, page=1):
        return {"results": search or [], "page": 1, "total_pages": 1,
                "total_results": len(search or [])}

    monkeypatch.setattr(main.tmdb, "tv_details", tv_details)
    monkeypatch.setattr(main.tmdb, "search_tv", search_tv)
    return TestClient(main.app)


DETALHE = {"id": 4242, "original_title": "Serie Exemplo",
           "localized_title": "A Série de Exemplo", "english_title": None,
           "original_language": "en", "year": "2024", "overview": "",
           "poster": None, "seasons": []}


# -------------------- nomes de arquivo --------------------

def test_episodios_do_nome_do_arquivo():
    f = catalog.episodes_in_filename
    assert f("Serie - S01E01 - Titulo WEBDL-1080p.mkv") == {(1, 1)}
    # multi-episódio: todos contam (o segundo pareceria faltando)
    assert f("Serie - S01E01-E02 - Titulo.mkv") == {(1, 1), (1, 2)}
    assert f("Serie S01E03E04.mkv") == {(1, 3), (1, 4)}
    assert f("Serie S02E05-06-07.mkv") == {(2, 5), (2, 6), (2, 7)}
    assert f("Serie S01E01-E03.mkv") == {(1, 1), (1, 2), (1, 3)}
    # o que NÃO é range: resolução, profundidade de cor, título após " - "
    assert f("Serie S01E07-1080p.mkv") == {(1, 7)}
    assert f("Serie S01E09-10bit.mkv") == {(1, 9)}
    assert f("Serie S01E01 - 02 Titulo.mkv") == {(1, 1)}
    # forma "1x02"; resolução não é episódio
    assert f("Serie 1x05 Titulo.mkv") == {(1, 5)}
    assert f("Serie 1920x1080.mkv") == set()
    # sem temporada no nome: só com a pasta "Season NN" dando a temporada
    assert f("Episode 03.mkv", season=2) == {(2, 3)}
    assert f("04 - Titulo.mkv", season=2) == {(2, 4)}
    assert f("04 - Titulo.mkv") == set()
    assert f("1080p.mkv", season=2) == set()
    assert f("E07 - Titulo.mkv", season=2) == {(2, 7)}
    assert f("Amor e 2 Amigos.mkv", season=2) == set()   # "e" é conjunção


def test_scan_de_serie_de_outro_programa(db, tmp_path):
    _dest(db, tmp_path / "tv")
    _serie_de_outro_programa(tmp_path / "tv")
    idx = catalog.series_index()
    assert len(idx) == 1
    e = idx[0]
    assert (e["folder"], e["norm_title"], e["year"], e["tmdb_id"]) == \
        ("Serie Exemplo", "serieexemplo", None, None)
    assert e["seasons"] == {1: set(range(1, 9))}  # 10 arquivos, 8 episódios


def test_pasta_sem_season_mas_com_episodios_conta(db, tmp_path):
    root = tmp_path / "tv"
    _dest(db, root)
    _files(root / "Serie Solta", "Serie Solta S01E01.mkv", "Serie Solta S01E02.mkv")
    _files(root / "Serie Sem SxxEyy", "Season 02/Episode 03.mkv",
           "Season 02/04 - Titulo.mkv")
    # num destino de séries, pasta sem episódio nem "Season" não é série
    _files(root / "Um Filme (2020)", "Um Filme (2020).mkv")
    _files(root / ".Trash-1000", "Serie S01E01.mkv")
    idx = {e["folder"]: e["seasons"] for e in catalog.series_index()}
    assert idx == {"Serie Solta": {1: {1, 2}},
                   "Serie Sem SxxEyy": {2: {3, 4}}}


def test_indice_de_series_nao_depende_do_cache_de_filmes(db, tmp_path):
    """O índice de séries refazia o scan a CADA chamada enquanto ninguém
    tivesse buscado filme (o cache de filmes vazio o tornava 'vencido')."""
    root = tmp_path / "tv"
    _dest(db, root)
    _files(root / "Serie A", "Season 01/Serie A S01E01.mkv")
    assert len(catalog.series_index()) == 1
    _files(root / "Serie B", "Season 01/Serie B S01E01.mkv")
    assert catalog._library_cache["at"] is None      # filmes nunca escaneados
    assert len(catalog.series_index()) == 1          # cache de séries vale
    catalog.invalidate_library()
    assert len(catalog.series_index()) == 2


# -------------------- casamento pasta x TMDB --------------------

def test_casa_pelo_titulo_original_sem_ano(db, tmp_path):
    _dest(db, tmp_path / "tv")
    _serie_de_outro_programa(tmp_path / "tv")
    # só o localizado (o que o card manda): não casa
    assert catalog.owned_episodes(4242, "A Série de Exemplo", "2024") == {}
    # com o original na lista: casa, mesmo a pasta sem ano
    owned = catalog.owned_episodes(4242, ["A Série de Exemplo", "Serie Exemplo"], "2024")
    assert owned == {1: list(range(1, 9))}


def test_ano_da_pasta_tem_que_bater(db, tmp_path):
    root = tmp_path / "tv"
    _dest(db, root)
    _files(root / "Serie Exemplo (1990)", "Season 01/Serie Exemplo S01E01.mkv")
    assert catalog.owned_episodes(4242, "Serie Exemplo", "2024") == {}
    assert catalog.owned_episodes(4242, "Serie Exemplo", "1990") == {1: [1]}


def test_id_marcado_manda(db, tmp_path):
    root = tmp_path / "tv"
    _dest(db, root)
    # título numa terceira grafia, mas marcada com o id: casa pelo id
    _files(root / "Outro Nome Qualquer [tmdbid-4242]", "Season 01/x S01E02.mkv")
    # marcada com OUTRO id e o mesmo título: é outra série (reboot homônimo)
    _files(root / "Serie Exemplo [tmdbid-777]", "Season 01/x S01E05.mkv")
    lib = catalog.series_in_library(4242, "Serie Exemplo", "2024")
    assert lib["seasons"] == {1: [2]}
    assert [(f["folder"], f["match"]) for f in lib["folders"]] == \
        [("Outro Nome Qualquer [tmdbid-4242]", "tmdbid")]
    # sem o id pedido (só título), a pasta marcada ainda casa pelo nome
    assert catalog.owned_episodes(None, "Serie Exemplo", None) == {1: [5]}


@pytest.mark.parametrize("pasta, titulo_tmdb", [
    ("S3R1E Exemplo", "Serie Exemplo"),               # dígito no lugar de letra
    ("Amor, Morte & Robos", "Amor Morte and Robos"),  # & x and
    ("Amor Morte and Robos", "Amor, Morte & Robos"),  # and x & (que some no exato)
    ("The Serie-X", "Serie-X"),                       # artigo inicial
    ("Serie-X, The", "The Serie-X"),                  # artigo no fim
    ("Serie Exemplo (US)", "Serie Exemplo"),          # desambiguador de país
])
def test_normalizacao_frouxa(db, tmp_path, pasta, titulo_tmdb):
    root = tmp_path / "tv"
    _dest(db, root)
    _files(root / pasta, "Season 01/a S01E01.mkv")
    lib = catalog.series_in_library(4242, [titulo_tmdb], "2024")
    assert lib["seasons"] == {1: [1]}
    assert lib["folders"][0]["match"] == "loose"


@pytest.mark.parametrize("pasta", ["Serie Exemplo!", "Serie: Exemplo", "SÉRIE EXEMPLO"])
def test_pontuacao_e_acento_casam_no_exato(db, tmp_path, pasta):
    root = tmp_path / "tv"
    _dest(db, root)
    _files(root / catalog.safe_name(pasta), "Season 01/a S01E01.mkv")
    assert catalog.series_in_library(4242, ["Serie Exemplo"], "2024")["folders"][0]["match"] \
        == "title"


def test_frouxa_nao_casa_serie_diferente(db, tmp_path):
    root = tmp_path / "tv"
    _dest(db, root)
    _files(root / "Serie Exemplo 2", "Season 01/a S01E01.mkv")
    _files(root / "Outra Serie", "Season 01/a S01E01.mkv")
    assert catalog.series_in_library(4242, ["Serie Exemplo"], "2024")["folders"] == []


def test_junta_pastas_de_dois_destinos(db, tmp_path):
    a = _dest(db, tmp_path / "tv1", label="Séries 1")
    b = _dest(db, tmp_path / "tv2", label="Séries 2")
    _files(tmp_path / "tv1" / "Serie Exemplo (2024) [tmdbid-4242]",
           "Season 02/Serie Exemplo (2024) S02E01 [pt+orig].mkv")
    _serie_de_outro_programa(tmp_path / "tv2")
    lib = catalog.series_in_library(4242, ["Serie Exemplo"], "2024")
    assert lib["seasons"] == {1: list(range(1, 9)), 2: [1]}
    assert sorted((f["destination_id"], f["match"], f["episodes"])
                  for f in lib["folders"]) == sorted([(a, "tmdbid", 1), (b, "title", 8)])


def test_destino_de_filmes_nao_conta_para_serie(db, tmp_path):
    _dest(db, tmp_path / "filmes", media="movie", label="Filmes")
    _serie_de_outro_programa(tmp_path / "filmes")
    assert catalog.series_in_library(4242, ["Serie Exemplo"], "2024")["folders"] == []


# -------------------- API: modal e busca --------------------

def test_owned_resolve_o_titulo_original_pelo_tmdb(db, tmp_path, monkeypatch):
    """A UI só manda o localizado; o backend busca o original no TMDB."""
    did = _dest(db, tmp_path / "tv")
    _serie_de_outro_programa(tmp_path / "tv")
    c = _client(monkeypatch, details=DETALHE)
    r = c.get("/api/series/4242/owned", params={"title": "A Série de Exemplo"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["seasons"] == {"1": list(range(1, 9))}
    assert body["folders"] == [{"folder": "Serie Exemplo", "destination_id": did,
                                "destination_label": "Séries", "match": "title",
                                "episodes": 8}]
    # o nome que o Outstasher daria: o que a UI oferece para padronizar
    assert body["canonical_name"] == "Serie Exemplo (2024) [tmdbid-4242]"


def test_owned_sem_tmdb_usa_o_que_a_ui_mandou(db, tmp_path, monkeypatch):
    _dest(db, tmp_path / "tv")
    _serie_de_outro_programa(tmp_path / "tv")
    c = _client(monkeypatch, details=None)  # TMDB fora do ar
    r = c.get("/api/series/4242/owned",
              params={"title": "A Série de Exemplo", "original_title": "Serie Exemplo",
                      "year": "2024"})
    assert r.json()["seasons"] == {"1": list(range(1, 9))}
    assert r.json()["canonical_name"] is None


def test_selo_da_busca_vem_do_destino_de_series(db, tmp_path, monkeypatch):
    _dest(db, tmp_path / "tv")
    _dest(db, tmp_path / "filmes", media="movie", label="Filmes")
    _serie_de_outro_programa(tmp_path / "tv")
    # pasta homônima no destino de FILMES: não faz a série "estar na coleção"
    (tmp_path / "filmes" / "Outra Serie (2020)").mkdir()
    c = _client(monkeypatch, search=[
        {"id": 4242, "title": "A Série de Exemplo", "original_title": "Serie Exemplo",
         "year": "2024", "media_type": "tv"},
        {"id": 5151, "title": "Outra Serie", "original_title": "Outra Serie",
         "year": "2020", "media_type": "tv"},
    ])
    res = {m["id"]: m for m in c.get("/api/series", params={"q": "serie"}).json()["results"]}
    assert res[4242]["in_catalog"] is True and res[4242]["owned_episodes"] == 8
    assert res[5151]["in_catalog"] is False and res[5151]["owned_episodes"] == 0


def test_busca_de_filmes_casa_pasta_marcada_pelo_id(db, tmp_path):
    """Pasta marcada com o título numa terceira língua: o id basta. E a pasta
    'Título (Ano)' sem id continua casando por título + ano."""
    _dest(db, tmp_path / "filmes", media="movie", label="Filmes")
    (tmp_path / "filmes" / "Exemplo Film (2014) [tmdbid-3131]").mkdir()
    (tmp_path / "filmes" / "Filme Exemplo (2010)").mkdir()
    keys = catalog.library_keys()
    assert catalog.in_library({"id": 3131, "original_title": "Ejemplo",
                               "title": "Filme de Exemplo", "year": "2014"}, keys)
    assert not catalog.in_library({"id": 3132, "original_title": "Ejemplo",
                                   "title": "Filme de Exemplo", "year": "2014"}, keys)
    assert catalog.in_library({"id": 9, "original_title": "Filme Exemplo",
                               "title": "Outro", "year": "2010"}, keys)


def test_padronizar_pasta_com_o_nome_do_outstasher(db, tmp_path, monkeypatch):
    did = _dest(db, tmp_path / "tv")
    _serie_de_outro_programa(tmp_path / "tv")
    c = _client(monkeypatch, details=DETALHE)
    r = c.post("/api/catalog/item/tmdbid", json={
        "folder": "Serie Exemplo", "destination_id": did, "tmdb_id": 4242,
        "title": "Serie Exemplo", "year": "2024"})
    assert r.status_code == 200, r.text
    assert r.json()["folder"] == "Serie Exemplo (2024) [tmdbid-4242]"
    assert (tmp_path / "tv" / "Serie Exemplo (2024) [tmdbid-4242]" / "Season 1").is_dir()
    # o índice foi invalidado: agora casa pelo id, e a pasta já é a canônica
    body = c.get("/api/series/4242/owned").json()
    assert [f["match"] for f in body["folders"]] == ["tmdbid"]
    assert body["folders"][0]["folder"] == body["canonical_name"]
    # sem título: comportamento antigo (título/ano da própria pasta)
    _files(tmp_path / "tv" / "Outra", "Season 01/a S01E01.mkv")
    assert catalog.tag_folder_with_tmdb(did, "Outra", 5) == "Outra [tmdbid-5]"


# -------------------- episódios em jobs ativos --------------------

def _ep(s, e, state):
    return {"season": s, "episode": e, "name": None, "air_date": "2020-01-01",
            "runtime": 40, "state": state, "src": {}, "output": None, "error": None}


def _job_tv(jid, tmdb_id, episodes, status="downloading", created="2026-01-01T00:00:00",
            **kw):
    job = {"id": jid, "media_type": "tv", "tmdb_id": tmdb_id, "status": status,
           "kind": "both", "mode": "auto", "created_at": created,
           "episodes": {f"S{s:02d}E{e:02d}": _ep(s, e, st) for s, e, st in episodes}}
    job.update(kw)
    jobs._jobs[jid] = job
    return job


def test_episodios_em_jobs_ativos(db, monkeypatch):
    _job_tv("tv1", 4242, [(1, 1, "done"), (1, 2, "downloading"), (1, 3, "pending"),
                          (1, 4, "merging"), (1, 5, "review"), (1, 6, "failed"),
                          (1, 7, "skipped_missing"), (1, 8, "skipped_future"),
                          (2, 1, "downloaded")])
    # mais novo, mesmo episódio: vale o mais antigo; e um episódio só dele
    _job_tv("tv2", 4242, [(1, 2, "pending"), (2, 2, "pending")], status="awaiting",
            created="2026-02-01T00:00:00", kind="original")
    # outra série, recompressão da mesma série e um filme com o mesmo id: fora
    _job_tv("tv3", 9999, [(1, 9, "downloading")])
    _job_tv("rc1", 4242, [(3, 1, "merging")], mode="recompress",
            recompress={"folder": "x", "rels": []})
    jobs._jobs["mv1"] = {"id": "mv1", "tmdb_id": 4242, "status": "downloading",
                         "created_at": "2026-01-01T00:00:00"}

    got = activity.episodes_in_jobs(4242)
    assert got["done"] == {1: [1]}
    assert {s: [(x["episode"], x["state"], x["job_id"]) for x in eps]
            for s, eps in got["seasons"].items()} == {
        1: [(2, "downloading", "tv1"), (3, "pending", "tv1"),
            (4, "merging", "tv1"), (5, "review", "tv1")],
        2: [(1, "downloaded", "tv1"), (2, "pending", "tv2")],
    }
    s2 = {x["episode"]: x for x in got["seasons"][2]}
    assert (s2[2]["job_status"], s2[2]["kind"]) == ("awaiting", "original")
    assert activity.episodes_in_jobs(1) == {"seasons": {}, "done": {}}


def test_endpoint_in_progress(db, monkeypatch):
    _job_tv("tv1", 4242, [(1, 1, "done"), (1, 2, "downloading")])
    c = _client(monkeypatch)
    r = c.get("/api/series/4242/in-progress")
    assert r.status_code == 200, r.text
    assert r.json() == {
        "seasons": {"1": [{"episode": 2, "state": "downloading", "job_id": "tv1",
                           "job_status": "downloading", "kind": "both"}]},
        "done": {"1": [1]},
    }


def test_job_de_serie_que_termina_em_erro_invalida_a_colecao(db, tmp_path):
    """Série entrega por episódio: o job abortado pela regra dos 75% (ou
    cancelado) pode ter deixado episódios no destino."""
    job = _job_tv("tve1", 4242, [(1, 1, "done"), (1, 2, "failed")], status="merging",
                  language="pt", detail="", output=None, progress={}, movie=None,
                  request={"seasons": [1], "episodes": {}}, awaiting=None,
                  order_map=None, torrents=[], search_queries=[],
                  search_candidates=[])
    catalog.series_index()
    catalog.library_keys()
    jobs._set(job, "error", "Abortado: só 1/2 episódio(s) concluído(s) (< 75%)")
    assert catalog._series_cache["at"] is None
    assert catalog._library_cache["at"] is None
    # filme em erro não entregou nada: o cache continua valendo
    catalog.series_index()
    filme = {"id": "mv2", "tmdb_id": 1, "language": "pt", "mode": "auto",
             "kind": "both", "status": "merging", "detail": "", "movie": None,
             "created_at": "2026-01-01T00:00:00", "output": None,
             "progress": {"video": None, "audio": None},
             "search_queries": [], "search_candidates": []}
    jobs._jobs["mv2"] = filme
    jobs._set(filme, "error", "falhou")
    assert catalog._series_cache["at"] is not None
