"""Escalares do job em colunas da `jobs` e metadados do TMDB em tabelas
compartilhadas (media / media_localized / media_seasons /
media_episode_groups), em vez de dentro de jobs.data.

Cobre: decomposição -> recomposição (round-trip do dict de filme, série,
conversão manual, recompressão, merge manual de série, pausa de drift),
presença de chave (ausente != None), o que não cabe no formato ficando no
documento, metadados compartilhados entre jobs (título localizado por
idioma, temporadas derivadas), progresso do merge efêmero, gravação só da
tabela que mudou, leitura do formato antigo (função pura e banco antigo) e
as consultas que liam o JSON (filtro de mídia, contagens, error_jobs_for).
"""
import asyncio
import json
import sqlite3

import pytest

import config
from services import jobs, store
from services.jobs import views
from services.series import pipeline

MOVIE = {"id": 900077, "original_title": "Filme Exemplo",
         "localized_title": "Filme Exemplo (BR)", "english_title": None,
         "original_language": "en", "year": "2014",
         "overview": "Sinopse de exemplo.",
         "poster": "https://image.tmdb.org/t/p/w342/exemplo-pt.jpg"}
PROG = {"pct": 100.0, "speed": 0, "eta": 8640000, "state": "uploading",
        "seeds": 4, "name": "Filme.Exemplo.2014.1080p", "size": 9000,
        "downloaded": 9000}


def _filme(jid="fx1", **kw) -> dict:
    job = {"id": jid, "tmdb_id": 900077, "language": "pt", "mode": "auto",
           "kind": "both", "skip_search": False, "advanced_merge": None,
           "download_only": False, "convert": None, "status": "done",
           "detail": "Concluído", "movie": dict(MOVIE),
           "progress": {"video": dict(PROG),
                        "audio": dict(PROG, name="Filme.Exemplo.Dublado")},
           "output": "/media/filmes/Filme Exemplo (2014)/Filme Exemplo (2014).mkv",
           "destination_id": 1, "destination_label": "Filmes",
           "destination_path": "/media/filmes", "torrent_target_id": 2,
           "torrent_target_label": "Padrão", "torrent_save_path": "/downloads",
           "torrent_local_path": "/mnt/downloads",
           "created_at": "2026-01-01T00:00:00",
           "search_queries": [], "search_candidates": [],
           "src_roots": {"video": "/downloads/Filme.Exemplo.2014.1080p",
                         "audio": "/downloads/Filme.Exemplo.Dublado"},
           "merge_started_at": "2026-01-01T01:00:00"}
    job.update(kw)
    return job


def _serie(jid="sx1", **kw) -> dict:
    job = {"id": jid, "media_type": "tv", "tmdb_id": 900091, "language": "pt",
           "mode": "manual", "kind": "both", "skip_search": False,
           "advanced_merge": {"cut_policy": "keep"}, "download_only": False,
           "convert": {"video_codec": "av1"}, "status": "awaiting",
           "detail": "Escolha os torrents",
           "movie": dict(MOVIE, id=900091, original_title="Serie Exemplo"),
           "request": {"seasons": [1], "episodes": {3: [2, 5]}},
           "episodes": {"S01E01": {"season": 1, "episode": 1, "name": "Ep 1",
                                   "air_date": "2020-01-01", "runtime": 40,
                                   "state": "pending", "src": {},
                                   "output": None, "error": None}},
           "torrents": [], "awaiting": {"reason": "manual_pick", "payload": None},
           "order_map": None, "progress": {}, "output": None,
           "search_queries": [], "search_candidates": [],
           "destination_id": 3, "destination_label": "Séries",
           "destination_path": "/media/series", "torrent_target_id": None,
           "torrent_target_label": None, "torrent_save_path": "",
           "torrent_local_path": "", "created_at": "2026-01-02T00:00:00",
           "known_seasons": [1, 2, 3],
           "episode_groups": [{"id": "g1", "name": "DVD", "type": 3,
                               "group_count": 3, "episode_count": 30}],
           "_phase2_done": True, "_gaps_accepted": True, "_manual_done": True,
           "_signals_accepted": True, "_rematch_done": True,
           "force_continue": True, "rematch_attempts": 2,
           "review_rules": [{"kind": "skip_intro"}],
           "merge_started_at": None}
    job.update(kw)
    return job


def _data(jid) -> dict:
    with store._lock:
        return json.loads(store._conn.execute(
            "SELECT data FROM jobs WHERE id = ?", (jid,)).fetchone()[0])


def _col(jid, col):
    with store._lock:
        return store._conn.execute(
            f"SELECT {col} FROM jobs WHERE id = ?", (jid,)).fetchone()[0]


def _rows(sql, *params):
    with store._lock:
        return store._conn.execute(sql, params).fetchall()


def _insert_doc(doc: dict):
    """Grava o documento cru em jobs.data (como um processo antigo deixou)."""
    with store._lock:
        store._conn.execute(
            "INSERT INTO jobs (id, created_at, status, data) VALUES (?, ?, ?, ?)",
            (doc["id"], doc["created_at"], doc["status"], json.dumps(doc)))
        store._conn.commit()


def _sem_merge(job: dict) -> dict:
    out = json.loads(json.dumps(job))
    out["progress"].pop("merge", None)
    return out


class _Sql:
    """Captura os comandos que o SQLite executa durante o bloco."""

    def __enter__(self):
        self.stmts: list[str] = []
        store._conn.set_trace_callback(self.stmts.append)
        return self

    def __exit__(self, *exc):
        store._conn.set_trace_callback(None)

    def writes(self) -> set[str]:
        out = set()
        for s in self.stmts:
            w = s.lstrip().split()
            if w and w[0].upper() in ("INSERT", "UPDATE", "DELETE"):
                out.add(w[2] if w[0].upper() in ("INSERT", "DELETE") else w[1])
        return out


# -------------------- round-trip --------------------

def test_filme_round_trip_e_documento_so_com_configuracao(temp_db):
    job = _filme()
    job["progress"]["merge"] = {"pct": 42.0, "phase": "convert"}
    store.upsert_job(job)
    # o documento guarda só configuração
    assert _data(job["id"]) == {"advanced_merge": None, "convert": None}
    back = store.get_job(job["id"])
    exp = {k: v for k, v in _sem_merge(job).items()
           if k not in ("search_queries", "search_candidates")}
    assert {k: v for k, v in back.items()
            if k not in ("search_queries", "search_candidates")} == exp
    # filme não tem media_type nem awaiting: a recomposição não inventa chave
    assert "media_type" not in back and "awaiting" not in back
    assert type(back["download_only"]) is bool and back["skip_search"] is False
    # o destino e o alvo dos torrents são snapshot em colunas
    assert _col(job["id"], "destination_path") == "/media/filmes"
    assert _col(job["id"], "torrent_target_label") == "Padrão"


def test_serie_round_trip(temp_db):
    job = _serie()
    store.upsert_job(job)
    data = _data(job["id"])
    assert data == {"advanced_merge": {"cut_policy": "keep"},
                    "convert": {"video_codec": "av1"},
                    "review_rules": [{"kind": "skip_intro"}]}
    back = store.get_job(job["id"])
    for k in ("search_queries", "search_candidates", "report"):
        back.pop(k, None)
    exp = {k: v for k, v in job.items()
           if k not in ("search_queries", "search_candidates")}
    assert back == exp
    # o pedido volta com as temporadas em int (como o create_series monta)
    assert back["request"]["episodes"] == {3: [2, 5]}
    assert _col(job["id"], "awaiting_reason") == "manual_pick"
    assert _col(job["id"], "rematch_attempts") == 2


def test_formas_especiais_round_trip(temp_db):
    """Conversão manual, recompressão (filme e lote de série), merge manual
    de série, pausa de drift (com e sem perfil) e alinhamento avançado."""
    manual = _filme("fm1", mode="files",
                    manual_files={"video": "/in/v.mkv", "audio": "/in/a.mkv"})
    for k in ("skip_search", "download_only", "torrent_target_id",
              "torrent_target_label", "torrent_save_path", "torrent_local_path"):
        manual.pop(k)
    recomp = _filme("fr1", mode="recompress", kind=None,
                    recompress={"folder": "Filme Exemplo (2014)",
                                "rel": "Filme Exemplo (2014).mkv", "replace": True})
    lote = _serie("sr1", mode="recompress", kind=None, request=None,
                  awaiting=None,
                  recompress={"folder": "Serie Exemplo (2020)",
                              "rels": ["Season 01/a.mkv", "Season 01/b.mkv"],
                              "replace": False})
    mserie = _serie("sm1", mode="files", request={"seasons": [], "episodes": {}},
                    manual_rows=[{"season": 1, "episode": 1, "original": "/o/1.mkv",
                                  "dubbed": "/d/1.mkv", "name": None},
                                 {"season": 1, "episode": 2, "original": "/o/2.mkv",
                                  "dubbed": "/d/2.mkv", "name": "Ep 2"}])
    drift = _filme("fd1", status="awaiting",
                   drift_confirm={"video_file": "/v.mkv", "audio_file": "/a.mkv",
                                  "tau1_ms": 120.5, "tau2_ms": 980.0,
                                  "profile": [{"t": 0.0, "offset_ms": 120.5,
                                               "quality": 9.1},
                                              {"t": 300.0, "offset_ms": 980.0,
                                               "quality": 8.7}],
                                  "verdict": "cut"})
    medindo = _filme("fd2", status="awaiting",
                     drift_confirm={"video_file": "/v.mkv", "audio_file": "/a.mkv",
                                    "tau1_ms": 1.0, "tau2_ms": 2.0})
    adv = _filme("fa1", status="merging",
                 advanced={"video_file": "/o.mkv", "audio_file": "/d.mkv"},
                 awaiting={"reason": "alignment_review", "payload": None})
    for job in (manual, recomp, lote, mserie, drift, medindo, adv):
        store.upsert_job(job)
        back = store.get_job(job["id"])
        exp = {k: v for k, v in json.loads(json.dumps(job)).items()
               if k not in ("search_queries", "search_candidates")}
        if job.get("media_type") == "tv":
            back.pop("report", None)
            exp["request"] = job["request"]
        got = {k: v for k, v in back.items()
               if k not in ("search_queries", "search_candidates")}
        assert got == exp, job["id"]
        # nada disso fica no documento
        assert set(_data(job["id"])) <= {"advanced_merge", "convert",
                                         "review_rules"}, job["id"]
    assert _rows("SELECT pos, rel FROM recompress_items WHERE job_id = 'sr1'") \
        == [(0, "Season 01/a.mkv"), (1, "Season 01/b.mkv")]
    assert len(_rows("SELECT * FROM manual_rows WHERE job_id = 'sm1'")) == 2
    assert len(_rows("SELECT * FROM drift_profile WHERE job_id = 'fd1'")) == 2
    assert "profile" not in store.get_job("fd2")["drift_confirm"]


def test_o_que_nao_cabe_fica_no_documento_e_volta_igual(temp_db):
    """Tipo diferente, forma diferente ou chave desconhecida: o valor fica em
    jobs.data como está (round-trip exato, nada se perde)."""
    job = _filme("fs1", tmdb_id="900077",            # texto: não cabe no INTEGER
                 progress={"video": 42.5, "audio": None},   # % solto (antigo)
                 chave_nova={"x": [1, 2]})
    job["movie"] = dict(MOVIE, seasons=[{"season": 1}])     # forma diferente
    store.upsert_job(job)
    data = _data(job["id"])
    for k in ("tmdb_id", "progress", "chave_nova", "movie"):
        assert data[k] == json.loads(json.dumps(job[k])), k
    assert _col(job["id"], "tmdb_id") is None
    back = store.get_job(job["id"])
    assert {k: back[k] for k in ("tmdb_id", "progress", "chave_nova", "movie")} \
        == {k: job[k] for k in ("tmdb_id", "progress", "chave_nova", "movie")}
    # pedido com lista vazia de episódios não tem linha que o represente
    serie = _serie("ss1", request={"seasons": [1], "episodes": {2: []}})
    store.upsert_job(serie)
    assert _data("ss1")["request"] == {"seasons": [1], "episodes": {"2": []}}


def test_chave_com_none_e_chave_ausente(temp_db):
    job = _filme("fn1", output=None, awaiting=None, merge_started_at=None)
    del job["skip_search"]
    store.upsert_job(job)
    back = store.get_job(job["id"])
    assert back["output"] is None and back["awaiting"] is None
    assert back["merge_started_at"] is None
    assert "skip_search" not in back and "media_type" not in back


# -------------------- metadados compartilhados --------------------

def test_metadados_do_tmdb_uma_vez_por_obra(temp_db):
    store.upsert_job(_filme("m1"))
    # mesmo filme, outro idioma: título e pôster do idioma, o resto é da obra
    es = dict(MOVIE, localized_title="Película Ejemplo",
              poster="https://image.tmdb.org/t/p/w342/ejemplo-es.jpg")
    store.upsert_job(_filme("m2", language="es", movie=es))
    # um retry do mesmo filme no mesmo idioma não duplica nada
    store.upsert_job(_filme("m3"))
    # série com o MESMO número no TMDB: espaço de ids diferente
    store.upsert_job(_serie("t1", tmdb_id=900077, movie=dict(MOVIE, original_title="Serie")))
    assert _rows("SELECT tmdb_id, media_type FROM media ORDER BY media_type") \
        == [(900077, "movie"), (900077, "tv")]
    assert _rows("SELECT language, localized_title FROM media_localized "
                 "WHERE media_type = 'movie' ORDER BY language") \
        == [("es", "Película Ejemplo"), ("pt", "Filme Exemplo (BR)")]
    assert store.get_job("m1")["movie"] == MOVIE
    assert store.get_job("m2")["movie"] == es
    assert store.get_job("t1")["movie"]["original_title"] == "Serie"
    # o TMDB mudou o ano: o job que buscou por último atualiza a obra, e o
    # título localizado do outro idioma continua o dele
    store.upsert_job(_filme("m4", movie=dict(MOVIE, year="2015")))
    assert store.get_job("m2")["movie"] == dict(es, year="2015")
    # job sem `movie` ainda (TMDB não respondeu) não pega o da obra
    store.upsert_job(_filme("m5", movie=None))
    assert store.get_job("m5")["movie"] is None


def test_temporadas_e_ordens_derivadas_da_serie(temp_db):
    a = _serie("ka", known_seasons=[1, 2])
    store.upsert_job(a)
    assert store.get_job("ka")["known_seasons"] == [1, 2]
    assert "known_seasons" not in _data("ka") and "episode_groups" not in _data("ka")
    # outro job da mesma série buscou depois: a série ganhou a temporada 3
    store.upsert_job(_serie("kb", known_seasons=[1, 2, 3], episode_groups=[]))
    assert store.get_job("ka")["known_seasons"] == [1, 2, 3]
    assert store.get_job("ka")["episode_groups"] == []
    # o job ativo regravado sem mexer nelas não devolve o dado velho
    full = store.get_job("ka")
    full["known_seasons"] = [1, 2]       # cópia velha em memória
    store._section_hash["ka"]["scalars"]["seasons"] = store._digest(
        (900091, (1, 2)))                    # = o que ele gravou da última vez
    store.upsert_job(full)
    assert _rows("SELECT season FROM media_seasons WHERE tmdb_id = 900091") \
        == [(1,), (2,), (3,)]
    # job de série sem as chaves (merge manual) continua sem elas
    m = _serie("kc")
    del m["known_seasons"], m["episode_groups"]
    store.upsert_job(m)
    back = store.get_job("kc")
    assert "known_seasons" not in back and "episode_groups" not in back


def test_falha_nas_ordens_alternativas_nao_apaga_as_da_serie(temp_db, monkeypatch):
    """_prepare: TMDB fora do ar nas ordens alternativas deixa o job sem a
    chave — um [] apagaria as que outro job da série já conhece."""
    async def details(tv_id, lang=None):
        return {**MOVIE, "id": tv_id, "seasons": [
            {"season": 0, "name": "Especiais", "episode_count": 1, "air_date": None},
            {"season": 1, "name": "T1", "episode_count": 1, "air_date": None}]}

    async def season(tv_id, s):
        return {"season": s, "episodes": [{"episode": 1, "name": "Ep",
                                           "air_date": "2020-01-01", "runtime": 40}]}

    async def groups(tv_id):
        raise RuntimeError("TMDB fora do ar")
    monkeypatch.setattr(pipeline.tmdb, "tv_details", details)
    monkeypatch.setattr(pipeline.tmdb, "tv_season", season)
    monkeypatch.setattr(pipeline.tmdb, "tv_episode_groups", groups)
    monkeypatch.setattr(pipeline.jobs, "_event", lambda *a, **k: None)
    job = _serie("pg1", episodes={}, request={"seasons": [1], "episodes": {}})
    del job["episode_groups"]
    asyncio.run(pipeline._prepare(job))
    assert "episode_groups" not in job and job["known_seasons"] == [1]


# -------------------- o que é gravado --------------------

def test_progresso_do_merge_e_efemero_o_do_download_nao(temp_db):
    job = _filme("pp1", status="downloading",
                 progress={"video": dict(PROG, pct=10.0), "audio": None})
    store.upsert_job(job)
    job["progress"]["merge"] = {"pct": 5.0, "phase": "align"}
    store.upsert_job(job)
    back = store.get_job("pp1")
    assert back["progress"] == {"video": dict(PROG, pct=10.0), "audio": None}
    # o watchdog anda: só a linha do jobs e o progresso são regravados
    job["progress"]["video"]["pct"] = 11.0
    with _Sql() as sql:
        store.upsert_job(job, create=False)
    assert sql.writes() == {"jobs", "job_progress"}
    assert store.get_job("pp1")["progress"]["video"]["pct"] == 11.0
    # nada mudou além do detalhe: nenhuma tabela filha
    job["detail"] = "Baixando..."
    with _Sql() as sql:
        store.upsert_job(job, create=False)
    assert sql.writes() == {"jobs"}


def test_hooks_do_ffmpeg_nao_gravam_o_job(temp_db):
    """A barra do merge é efêmera: andar de 0 a 4% não escreve nada no banco
    (observado no SQL executado, não interceptando função do store)."""
    job = _filme("ph1", status="merging")
    _log, on_progress = jobs.runtime._ffmpeg_hooks(job)
    with _Sql() as sql:
        for i in range(5):
            on_progress({"pct": float(i)})
    assert sql.writes() == set() and job["progress"]["merge"]["pct"] == 4.0


def test_create_false_sem_linha_nao_grava_nada(temp_db):
    store.upsert_job(_serie("cf1"), create=False)
    for t in ("jobs", "media", "media_localized", "media_seasons",
              "media_episode_groups", "job_requests"):
        assert _rows(f"SELECT COUNT(*) FROM {t}") == [(0,)], t


def test_remover_job_leva_as_linhas_filhas(temp_db):
    store.upsert_job(_filme("rm1"))
    store.upsert_job(_serie("rm2", manual_rows=[]))
    store.delete_job("rm1")
    store.delete_job("rm2")
    for t in store._KID_TABLES:
        assert _rows(f"SELECT COUNT(*) FROM {t}") == [(0,)], t
    # o metadado é da obra, não do job: fica
    assert _rows("SELECT COUNT(*) FROM media") == [(2,)]


# -------------------- consultas --------------------

def test_filtros_e_contagens_pela_coluna(temp_db):
    store.upsert_job(_filme("q1"))
    store.upsert_job(_filme("q2", status="error", created_at="2026-01-03T00:00:00"))
    store.upsert_job(_serie("q3", status="done"))
    assert store.count_jobs_by_status("tv") == {"done": 1}
    assert store.count_jobs_by_status("movie") == {"done": 1, "error": 1}
    T = ("done", "error", "cancelled")
    # com e sem paginação (o SQL das duas formas)
    assert [j["id"] for j in store.load_jobs_by_status(T, media="movie")] == ["q2", "q1"]
    assert [j["id"] for j in store.load_jobs_by_status(T, 1, 1, media="movie")] == ["q1"]
    assert [j["id"] for j in store.load_jobs_by_status(T, media="tv")] == ["q3"]
    with store._lock:
        plan = " ".join(r[3] for r in store._conn.execute(
            "EXPLAIN QUERY PLAN SELECT status, COUNT(*) FROM jobs "
            "WHERE media_type = ? GROUP BY status", ("tv",)))
    assert "idx_jobs_media" in plan


def test_error_jobs_for(temp_db):
    store.upsert_job(_filme("e1", status="error"))
    store.upsert_job(_filme("e2", status="error", kind="dubbed"))
    store.upsert_job(_filme("e3", status="done"))
    old = _filme("e4", status="error")
    del old["kind"]                      # anterior à opção: conta como both
    store.upsert_job(old)
    store.upsert_job(_filme("e5", status="error", kind=None, mode="recompress"))
    store.upsert_job(_filme("e6", status="error", language="es"))
    assert sorted(store.error_jobs_for(900077, "pt", "both")) == ["e1", "e4"]
    assert store.error_jobs_for(900077, "pt", "dubbed") == ["e2"]


def test_summary_e_all_paginado_iguais_ao_jeito_antigo(temp_db):
    for i, st in enumerate(("done", "error", "cancelled", "done", "error")):
        store.upsert_job(_filme(f"h{i}", status=st,
                                created_at=f"2026-01-0{i + 1}T00:00:00"))
    ativo = _filme("a1", status="downloading", created_at="2026-01-04T12:00:00")
    jobs._jobs["a1"] = ativo
    ids = {x["id"] for x in views.summary()}
    assert ids == {"a1", "h1", "h4"}            # concluído/cancelado não entram
    inteiro = [x["id"] for x in views.list_group("all")["items"]]
    for per_page in (1, 2, 4):
        paginas = []
        for page in range(1, 8):
            r = views.list_group("all", page, per_page)
            assert r["total"] == 6
            paginas += [x["id"] for x in r["items"]]
        assert paginas == inteiro


def test_retry_da_serie_pede_o_mesmo_que_o_original(temp_db, monkeypatch):
    """O pedido não é derivável dos episódios: o retry pede a TEMPORADA
    inteira de novo (episódios lançados depois entram) e os avulsos."""
    got = {}

    async def fake_create(tmdb_id, language, seasons=None, episodes=None, **kw):
        got.update(seasons=seasons, episodes=episodes)
        return {}
    monkeypatch.setattr(pipeline, "create_series", fake_create)
    store.upsert_job(_serie("rt1", status="error"))
    asyncio.run(pipeline.retry(store.get_job("rt1")))
    assert got == {"seasons": [1], "episodes": {3: [2, 5]}}


def test_gates_derivados_iguais_aos_do_pipeline():
    assert set(store._DERIVED_TV_GATES) == set(pipeline.DERIVED_GATES)


# -------------------- formato antigo --------------------

def _doc_antigo_serie() -> dict:
    doc = _serie("old1")
    doc.pop("search_queries")
    doc.pop("search_candidates")
    doc["search_tv"] = {"original": {}, "dubbed": {}}
    doc["awaiting"] = {"reason": "gaps_confirm", "payload": {"missing": ["x"]}}
    doc["progress"] = {"merge": {"pct": 3.0}}
    doc["report"] = {"attempted": 1}
    return json.loads(json.dumps(doc))       # como estava no banco (JSON)


def test_normalizacao_legada_pura():
    doc = _doc_antigo_serie()
    antes = json.dumps(doc)
    out = store._legacy_scalars(doc)
    assert json.dumps(doc) == antes                       # não mexe na entrada
    assert out["request"] == {"seasons": [1], "episodes": {3: [2, 5]}}
    assert out["progress"] == {}
    assert out["awaiting"] == {"reason": "gaps_confirm", "payload": None}
    assert out["known_seasons"] == [1, 2, 3] and out["_phase2_done"] is True
    f = json.loads(json.dumps(_filme("oldf")))
    f["progress"]["merge"] = {"pct": 50.0}
    f["advanced"] = {"video_file": "/o", "audio_file": "/d", "edl": {"version": 3}}
    out = store._legacy_scalars(f)
    assert "merge" not in out["progress"]
    assert out["advanced"]["edl"] == {"version": 3}       # a EDL fica (seção própria)
    assert out["movie"] == MOVIE


def test_formato_antigo_de_movie_e_progress_vai_para_as_tabelas(temp_db):
    """Caso real (banco do servidor): jobs de versões antigas tinham `movie`
    sem english_title/original_language/overview (ou com `seasons`, lista que
    nada lê mais) e o progresso do download sem size/downloaded. O formato
    exato do codificador os deixava inteiros no documento; normalizados, vão
    para as tabelas como qualquer job novo."""
    f = json.loads(json.dumps(_filme("velho")))
    f["movie"] = {"id": f["tmdb_id"], "original_title": "Filme Exemplo",
                  "localized_title": "Filme Exemplo", "year": "2001",
                  "poster": "http://p/x.jpg"}
    for role in ("video", "audio"):
        f["progress"][role] = {"pct": 100.0, "speed": 0, "eta": 8640000,
                               "state": "uploading", "seeds": 0, "name": "x"}
    f["movie"]["seasons"] = [{"season_number": 1}]
    out = store._legacy_scalars(f)
    assert out["movie"]["overview"] is None and "seasons" not in out["movie"]
    assert out["progress"]["video"]["size"] is None
    store.upsert_job(out)
    assert set(_data("velho")) <= {"advanced_merge", "convert", "review_rules"}
    back = store.get_job("velho")
    assert back["movie"]["original_title"] == "Filme Exemplo"
    assert back["progress"]["video"]["pct"] == 100.0


def test_banco_antigo_abre_lista_e_migra_no_upsert(temp_db):
    doc = _doc_antigo_serie()
    _insert_doc(doc)
    filme = json.loads(json.dumps(_filme("oldm", status="error")))
    filme.pop("search_queries"), filme.pop("search_candidates")
    filme["search"] = {"audio": [], "video": []}
    _insert_doc(filme)
    # init() de novo: a coluna de mídia das linhas antigas é preenchida
    store._conn.close()
    store._conn = None
    store.init()
    assert store.count_jobs_by_status("tv") == {"awaiting": 1}
    assert [j["id"] for j in store.load_jobs_by_status(("awaiting",), media="tv")] \
        == ["old1"]
    assert store.error_jobs_for(900077, "pt", "both") == ["oldm"]
    job = store.get_job("old1")
    assert job["request"]["episodes"] == {3: [2, 5]}
    assert job["awaiting"]["payload"] is None and "report" not in job
    # regravado (job ativo): sai no formato novo e volta igual
    store.upsert_job(job)
    assert _col("old1", "keys") is not None
    assert set(_data("old1")) == {"advanced_merge", "convert", "review_rules"}
    back = store.get_job("old1")
    back.pop("report", None)
    job.pop("report", None)
    assert back == job


def test_indice_antigo_de_midia_vira_o_da_coluna(tmp_path, monkeypatch):
    """Banco de antes deste formato: jobs sem as colunas e o índice de mídia
    sobre json_extract. O init acrescenta as colunas e troca o índice."""
    db = tmp_path / "jobs.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, created_at TEXT NOT NULL, "
                "status TEXT NOT NULL, data TEXT NOT NULL)")
    con.execute("CREATE INDEX idx_jobs_media ON jobs("
                "IFNULL(json_extract(data, '$.media_type'), 'movie'))")
    con.execute("INSERT INTO jobs VALUES ('v1', '2026-01-01', 'done', ?)",
                (json.dumps({"id": "v1", "media_type": "tv", "status": "done",
                             "created_at": "2026-01-01", "tmdb_id": 5,
                             "language": "pt", "progress": {}}),))
    con.commit()
    con.close()
    monkeypatch.setenv("DB_DIR", str(tmp_path))
    import importlib
    importlib.reload(config)
    importlib.reload(store)
    store.init()
    with store._lock:
        sql = store._conn.execute("SELECT sql FROM sqlite_master WHERE "
                                  "name = 'idx_jobs_media'").fetchone()[0]
    assert "json_extract" not in sql and "media_type" in sql
    assert store.count_jobs_by_status("tv") == {"done": 1}
    assert store.get_job("v1")["media_type"] == "tv"


@pytest.fixture(autouse=True)
def _sem_jobs_em_memoria():
    yield
    jobs._jobs.clear()
