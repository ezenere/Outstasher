"""Candidatos de busca em tabelas (search_queries / search_candidates /
candidate_episodes) em vez de cópias no documento do job.

Cobre: decomposição -> recomposição (round-trip do dict), gravação só quando
os candidatos mudam, job apagado não ressuscita linhas, leitura do formato
antigo (search/fallbacks/current/search_tv e o rastro inline dos eventos), a
busca de filme gravando UMA linha por candidato, o ranking de série com o
pack repetido virando um candidato só, e o watchdog achando a próxima reserva
depois de um restart (filme e série).
"""
import asyncio
import json

import pytest

from services import candidates, jobs, store
from services.jobs import downloads, search
from services.series import pipeline


def _movie_job(jid="mv1", **extra) -> dict:
    job = {"id": jid, "tmdb_id": 7, "language": "pt", "mode": "manual",
           "kind": "both", "status": "searching", "detail": "", "output": None,
           "movie": None, "created_at": "2026-01-01T00:00:00",
           "progress": {"video": None, "audio": None},
           "search_queries": [], "search_candidates": []}
    job.update(extra)
    return job


def _cand(cid, role, title, **kw):
    src = {"title": title, "tracker": "t1", "seeders": 5, "size": 100,
           "edition": None, "quality": "1080p WEB-DL", "score": 28.0,
           "magnet": f"magnet:?xt=urn:btih:{cid}", "link": None}
    src.update({k: v for k, v in kw.items() if k in candidates.DATA_FIELDS})
    pos = {k: v for k, v in kw.items() if k not in candidates.DATA_FIELDS}
    return candidates.new(role, cid, src, **pos)


class FakeQbit:
    def __init__(self):
        self.added = []

    async def info_by_tag(self, tag):
        return []

    async def remove_tag(self, h, tag):
        pass

    async def delete(self, h, delete_files=False):
        pass

    async def add(self, url, tag, save_path=None):
        self.added.append((url, tag))


def _ids_rows(jid):
    with store._lock:
        return [r[0] for r in store._conn.execute(
            "SELECT id FROM search_candidates WHERE job_id = ? ORDER BY id", (jid,))]


# -------------------- round-trip e gravação --------------------

def test_round_trip_filme_e_serie(temp_db):
    job = _movie_job()
    n = candidates.add_query(job, "audio", "Filme Teste 2014", 12)
    job["search_candidates"] += [
        _cand("a0", "audio", "Filme Teste 2014 Dublado", query=n, trace=0, rank=0,
              active=True, year_match=True, rejected=None, chosen=False, tier=0),
        _cand("a1", "audio", "Filme Teste 2014 Dual", query=n, trace=1, rank=1,
              fallback=0, year_match=False),
        _cand(f"q{n}.2", "audio", "Filme Teste CAM", query=n, trace=2,
              rejected="qualidade muito baixa (CAM/TS)", score=None),
        _cand("custom:1", "video", "Filme Exemplo 2160p", tracker="manual",
              seeders=None, rank=0),
    ]
    store.upsert_job(job)
    back = store.get_job(job["id"])
    assert back["search_queries"] == job["search_queries"]
    assert back["search_candidates"] == job["search_candidates"]

    serie = {"id": "tv1", "media_type": "tv", "status": "awaiting",
             "created_at": "2026-01-01T00:00:00", "search_queries": [],
             "search_candidates": []}
    pack = {"title": "Serie Exemplo S01 1080p", "tracker": "t", "seeders": 3,
            "size": 900, "quality": "1080p WEB-DL", "coverage": "S01 completa",
            "score": 28.0, "tier": 28, "magnet": "magnet:?xt=urn:btih:p",
            "link": None, "infohash": "abc", "files": 10}
    avulso = dict(pack, title="Serie Exemplo S01E02 720p", coverage="S01E02",
                  magnet="magnet:?xt=urn:btih:e2", files=1)
    candidates.set_series(serie, "original", {"S01E01": [pack],
                                              "S01E02": [avulso, pack]})
    store.upsert_job(serie)
    back = store.get_job("tv1")
    assert back["search_candidates"] == serie["search_candidates"]
    # o pack é UMA linha, com a posição em cada episódio
    assert len(back["search_candidates"]) == 2
    assert [c["title"] for c in candidates.for_episode(back, "original", "S01E02")] == [
        "Serie Exemplo S01E02 720p", "Serie Exemplo S01 1080p"]


def test_so_regrava_candidatos_quando_mudam(temp_db):
    job = _movie_job(status="downloading")
    job["search_candidates"] = [_cand("v0", "video", "Filme Exemplo 1080p", rank=0)]
    store.upsert_job(job)
    antes = _ids_rows(job["id"])
    # progresso/status mudou, candidatos não: as linhas ficam como estão
    job["detail"] = "baixando 50%"
    store.upsert_job(job, create=False)
    assert _ids_rows(job["id"]) == antes
    # candidato mudou (virou o ativo): reescreve
    candidates.set_active(job, "video", job["search_candidates"][0])
    store.upsert_job(job, create=False)
    assert _ids_rows(job["id"]) != antes
    assert candidates.active(store.get_job(job["id"]), "video")["id"] == "v0"


def test_job_apagado_nao_ressuscita_nem_as_linhas_filhas(temp_db):
    job = _movie_job()
    n = candidates.add_query(job, "video", "Filme Exemplo 2014", 1)
    job["search_candidates"] = [_cand("v0", "video", "Filme Exemplo", query=n,
                                      trace=0, rank=0)]
    store.upsert_job(job)
    store.delete_job(job["id"])
    with store._lock:
        for t in ("search_queries", "search_candidates", "candidate_episodes"):
            assert store._conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] == 0
    # o flush atrasado (create=False) de um job removido não insere nada
    job["search_candidates"].append(_cand("v1", "video", "outro", rank=1))
    store.upsert_job(job, create=False)
    assert store.get_job(job["id"]) is None
    with store._lock:
        assert store._conn.execute(
            "SELECT COUNT(*) FROM search_candidates").fetchone()[0] == 0


def test_foreign_keys_ligadas(temp_db):
    with store._lock:
        assert store._conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


# -------------------- busca de filme --------------------

def _hit(title, seeders=10, size=1000, tracker="t1"):
    return {"title": title, "seeders": seeders, "size": size,
            "magnet": f"magnet:?xt=urn:btih:{abs(hash(title)) % 10 ** 12:040x}",
            "link": None, "tracker": tracker, "tracker_id": tracker,
            "files": None, "infohash": None}


@pytest.fixture
def busca_falsa(monkeypatch):
    por_query = {
        "Filme Exemplo 2014": [
            _hit("Filme Exemplo 2014 2160p BluRay REMUX", size=5000),
            _hit("Filme Exemplo 2014 1080p WEB-DL", size=2000),
            _hit("Filme Exemplo 2014 1080p WEB-DL DUAL Dublado", size=2100),
            _hit("Filme Exemplo 2014 HDCAM"),
            _hit("Filme Exemplo 2014 720p", seeders=0),
        ],
        "Filme Teste 2014": [
            _hit("Filme Teste 2014 1080p Dublado", size=1500),
            _hit("Outro Filme 2014 1080p"),
        ],
    }

    async def fake_search(query, indexer="all", categories=None):
        return list(por_query.get(query, []))

    async def fake_details(tmdb_id, lang):
        return {"id": tmdb_id, "original_title": "Filme Exemplo",
                "localized_title": "Filme Teste", "english_title": None,
                "original_language": "en", "year": "2014", "overview": None,
                "poster": None}
    monkeypatch.setattr(search.jackett, "search", fake_search)
    monkeypatch.setattr(search.tmdb, "details", fake_details)


def test_busca_de_filme_grava_uma_linha_por_candidato(temp_db, busca_falsa):
    job = _movie_job()
    jobs._jobs[job["id"]] = job
    asyncio.run(search._search(job))
    jobs.flush_dirty(force=True)

    qs = job["search_queries"]
    assert [(q["role"], q["query"]) for q in qs] == [
        ("audio", "Filme Teste 2014"), ("audio", "Filme Exemplo 2014"),
        ("video", "Filme Exemplo 2014")]
    # o viável escolhível É a linha do rastro (nada de segunda cópia)
    for c in candidates.selectable(job, "video"):
        assert c["query"] == qs[2]["n"] and c["trace"] is not None
        assert c["magnet"]
    ids = [c["id"] for c in job["search_candidates"]]
    assert len(ids) == len(set(ids))
    assert [c["id"] for c in candidates.selectable(job, "video")] == ["v0", "v1", "v2"]
    assert candidates.selectable(job, "video")[0]["title"].endswith("REMUX")
    # áudio: o título no idioma dublado na frente
    assert candidates.selectable(job, "audio")[0]["title"] == "Filme Teste 2014 1080p Dublado"

    # o evento só referencia a consulta; o detalhe resolve o rastro
    with store._lock:
        raw = [json.loads(r[0]) for r in store._conn.execute(
            "SELECT data FROM events WHERE job_id = ? AND kind = 'candidates'",
            (job["id"],))]
    assert all("candidates" not in d and "search_query" in d for d in raw)
    detail = jobs.get_job(job["id"])
    evs = [e for e in detail["events"] if e["kind"] == "candidates"]
    assert len(evs) == 3
    video_trace = evs[2]["data"]["candidates"]
    assert len(video_trace) == 5
    assert set(video_trace[0]) == {"title", "tracker", "seeders", "size", "edition",
                                   "quality", "score", "rejected", "chosen",
                                   "year_match"}
    motivos = {c["title"]: c["rejected"] for c in video_trace}
    assert motivos["Filme Exemplo 2014 720p"] == "sem seeders"
    assert "CAM" in motivos["Filme Exemplo 2014 HDCAM"]
    # a lista de escolha manual no formato que o JobDetail lê
    assert [c["id"] for c in detail["search"]["video"]] == ["v0", "v1", "v2"]
    assert set(detail["search"]["video"][0]) == {
        "id", "title", "tracker", "seeders", "size", "edition", "quality",
        "score", "magnet", "link"}
    # e o banco devolve exatamente o que estava em memória
    back = store.get_job(job["id"])
    assert back["search_candidates"] == job["search_candidates"]
    jobs._jobs.pop(job["id"], None)


# -------------------- watchdog (filme) --------------------

def test_watchdog_de_filme_acha_a_proxima_reserva_depois_do_restart(temp_db, monkeypatch):
    qbit = FakeQbit()
    monkeypatch.setattr(jobs.runtime, "_qbit", qbit)
    job = _movie_job(kind="original", status="awaiting")
    job["search_candidates"] = [
        _cand("v0", "video", "Filme Exemplo 2160p", rank=0),
        _cand("v1", "video", "Filme Exemplo 1080p", rank=1),
        _cand("v2", "video", "Filme Exemplo Extended 1080p", rank=2, edition="extended"),
        _cand("v3", "video", "Filme Exemplo 720p", rank=3),
    ]
    jobs._jobs[job["id"]] = job
    v0 = job["search_candidates"][0]
    asyncio.run(downloads._start_download(job, None, v0))
    # reservas = mesmo corte, na ordem da busca; o corte diferente fica de fora
    assert [c["id"] for c in candidates.fallbacks(job, "video")] == ["v1", "v3"]

    # restart: o job volta do banco
    jobs.flush_dirty(force=True)
    jobs._jobs.clear()
    job = store.get_job(job["id"])
    jobs._jobs[job["id"]] = job
    assert candidates.active(job, "video")["id"] == "v0"

    travado = {"hash": "h", "name": "Filme Exemplo 2160p"}
    assert asyncio.run(downloads._switch_torrent(job, "video", travado, 30))
    assert candidates.active(job, "video")["id"] == "v1"
    assert qbit.added[-1][0] == "magnet:?xt=urn:btih:v1"
    # o que saiu volta para o FIM da fila
    assert [c["id"] for c in candidates.fallbacks(job, "video")] == ["v3", "v0"]
    assert asyncio.run(downloads._switch_torrent(job, "video", travado, 30))
    assert candidates.active(job, "video")["id"] == "v3"
    assert jobs._public(job)["video_torrent"]["title"] == "Filme Exemplo 720p"
    # a fila persiste com a troca
    jobs.flush_dirty(force=True)
    back = store.get_job(job["id"])
    assert [c["id"] for c in candidates.fallbacks(back, "video")] == ["v0", "v1"]
    jobs._jobs.clear()


def test_select_com_magnet_proprio_entra_no_topo_e_vira_reserva_depois(temp_db):
    job = _movie_job(status="awaiting")
    job["search_candidates"] = [_cand("v0", "video", "Filme Exemplo 1080p", rank=0)]
    c = candidates.add_custom(job, "video", search.custom_candidate(
        "magnet:?xt=urn:btih:meu", "Filme Exemplo 2160p"))
    assert [x["id"] for x in candidates.selectable(job, "video")] == [c["id"], "v0"]
    # o mesmo link de novo não duplica
    candidates.add_custom(job, "video", dict(c, title="outro nome"))
    assert len(candidates.selectable(job, "video")) == 2
    assert candidates.selectable(job, "video")[0]["title"] == "outro nome"


# -------------------- série --------------------

def _serie_job(jid="tv2"):
    return {
        "id": jid, "media_type": "tv", "tmdb_id": 9, "language": "pt",
        "mode": "auto", "kind": "original", "status": "searching", "detail": "",
        "output": None, "progress": {}, "created_at": "2026-01-01T00:00:00",
        "movie": {"original_title": "Serie Exemplo", "localized_title": "Serie Exemplo",
                  "english_title": None, "year": "2020"},
        "known_seasons": [1],
        "episodes": {k: {"season": 1, "episode": e, "name": k, "air_date": "2020-01-01",
                         "runtime": 40, "state": "pending", "src": {},
                         "output": None, "error": None}
                     for e, k in ((1, "S01E01"), (2, "S01E02"))},
        "torrents": [], "awaiting": None, "report": None, "order_map": None,
        "search_queries": [], "search_candidates": [],
    }


def test_rank_de_serie_pack_repetido_vira_um_candidato(temp_db):
    job = _serie_job()
    pipeline._pools[job["id"]] = {"original": [
        _hit("Serie Exemplo S01 1080p WEB-DL", size=9000),
        _hit("Serie Exemplo S01 720p WEB-DL", size=4000),
        _hit("Serie Exemplo S01E01 1080p WEB-DL", size=900),
        _hit("Serie Exemplo S01E02 1080p WEB-DL", size=900),
    ], "dubbed": []}
    try:
        ranked = pipeline._rank_all(job)
    finally:
        pipeline._pools.pop(job["id"], None)
    rows = candidates.of_role(job, "original")
    assert len(rows) == 4                    # antes: 2 packs × 2 eps + 2 avulsos = 6
    for (s, e), lista in ranked["original"].items():
        key = pipeline.ep_key(s, e)
        assert [c["title"] for c in candidates.for_episode(job, "original", key)] == [
            c["title"] for c in lista]
    pack = next(c for c in rows if c["title"] == "Serie Exemplo S01 1080p WEB-DL")
    assert set(pack["episodes"]) == {"S01E01", "S01E02"}
    assert pack["id"] == "o0"
    # a escolha manual e a troca acham o candidato pelo id novo
    assert pipeline._find_candidate(job, "original", "o0") is pack


def test_watchdog_de_serie_acha_reserva_depois_do_restart(temp_db, monkeypatch):
    qbit = FakeQbit()
    # o pipeline de séries usa a referência da fachada (jobs._qbit)
    monkeypatch.setattr(jobs, "_qbit", qbit)
    job = _serie_job()
    job["status"] = "downloading"
    pack = {"title": "Serie Exemplo S01 1080p", "tracker": "t", "seeders": 3,
            "size": 900, "quality": "1080p", "coverage": "S01 completa",
            "score": 28.0, "tier": 28, "magnet": "magnet:?xt=urn:btih:" + "a" * 40,
            "link": None, "infohash": None, "files": None}
    parcial = dict(pack, title="Serie Exemplo S01E01 1080p",
                   magnet="magnet:?xt=urn:btih:" + "b" * 40)
    reserva = dict(pack, title="Serie Exemplo S01 720p",
                   magnet="magnet:?xt=urn:btih:" + "c" * 40)
    candidates.set_series(job, "original", {"S01E01": [pack, parcial, reserva],
                                            "S01E02": [pack, reserva]})
    job["torrents"] = [{
        "n": 0, "role": "original", "tag": "dl-tv2-t0", "title": pack["title"],
        "tracker": "t", "seeders": 3, "size": 900, "quality": "1080p",
        "coverage_label": "S01 completa", "magnet": pack["magnet"], "link": None,
        "infohash": None, "files_count": None, "coverage": ["S01E01", "S01E02"],
        "state": "downloading", "hash": "h0", "progress": None,
        "selected_files": None, "content_path": None}]
    store.upsert_job(job)

    # restart: candidatos voltam do banco e o watchdog acha a reserva
    job = store.get_job("tv2")
    jobs._jobs["tv2"] = job
    t = job["torrents"][0]
    assert asyncio.run(pipeline._switch_stalled(job, t, 30))
    assert t["title"] == "Serie Exemplo S01 720p"     # pulou o avulso parcial
    assert qbit.added[-1][0] == reserva["magnet"]
    # a lista do "Escolher outro…" também sai dos candidatos persistidos
    alts = pipeline.torrent_alternatives("tv2", 0)
    assert [a["title"] for a in alts] == ["Serie Exemplo S01 1080p"]
    jobs._jobs.clear()


# -------------------- formato antigo --------------------

def _grava_doc_antigo(doc: dict):
    with store._lock:
        store._conn.execute(
            "INSERT INTO jobs (id, created_at, status, data) VALUES (?, ?, ?, ?)",
            (doc["id"], doc["created_at"], doc["status"], json.dumps(doc)))
        store._conn.commit()


def _slim_antigo(cid, title, edition=None):
    return {"id": cid, "title": title, "tracker": "t1", "seeders": 5, "size": 100,
            "edition": edition, "score": 28.0,
            "magnet": f"magnet:?xt=urn:btih:{cid}", "link": None}


def test_le_filme_no_formato_antigo(temp_db):
    a0, a1 = _slim_antigo("a0", "Filme Teste Dublado"), _slim_antigo("a1", "Filme Teste Dual")
    v0, v1 = _slim_antigo("v0", "Filme Exemplo 2160p"), _slim_antigo("v1", "Filme Exemplo 1080p")
    doc = {"id": "old1", "tmdb_id": 7, "language": "pt", "mode": "auto",
           "kind": "both", "status": "downloading", "detail": "",
           "created_at": "2026-01-01T00:00:00", "output": None, "movie": None,
           "progress": {"video": None, "audio": None},
           "search": {"audio": [a0, a1], "video": [v0, v1]},
           "fallbacks": {"audio": [a1], "video": [v1]},
           "current": {"audio": a0, "video": v0},
           "audio_torrent": {"title": a0["title"], "seeders": 5, "size": 100,
                             "score": 28.0, "edition": None, "id": "a0", "tracker": "t1"},
           "video_torrent": {"title": v0["title"], "seeders": 5, "size": 100,
                             "score": 28.0, "edition": None, "id": "v0", "tracker": "t1"}}
    _grava_doc_antigo(doc)

    job = store.get_job("old1")
    assert not any(k in job for k in store._LEGACY_KEYS)
    # 4 candidatos únicos: as reservas e o ativo eram cópias
    assert len(job["search_candidates"]) == 4
    assert [c["id"] for c in candidates.fallbacks(job, "audio")] == ["a1"]
    assert candidates.active(job, "video")["id"] == "v0"
    # a UI recebe o mesmo de antes
    view = candidates.search_view(job)
    assert [c["id"] for c in view["audio"]] == ["a0", "a1"]
    assert view["video"][1] == {**v1, "quality": None}
    pub = jobs._public(job)
    assert pub["video_torrent"] == doc["video_torrent"]
    assert pub["current"]["audio"]["id"] == "a0"

    # job ativo regravado sai no formato novo (data sem as cópias)
    store.upsert_job(job)
    with store._lock:
        data = json.loads(store._conn.execute(
            "SELECT data FROM jobs WHERE id = 'old1'").fetchone()[0])
    assert not any(k in data for k in store._LEGACY_KEYS)
    assert store.get_job("old1")["search_candidates"] == job["search_candidates"]


def test_le_filme_antigo_sem_current(temp_db):
    """Jobs anteriores ao `current`: o ativo é achado pelo título escolhido; se
    nem estiver na busca, vira um candidato sem link (o detalhe mostra)."""
    v0 = _slim_antigo("v0", "Filme Exemplo 1080p")
    doc = {"id": "old2", "status": "done", "created_at": "2026-01-01T00:00:00",
           "search": {"audio": [], "video": [v0]}, "fallbacks": None, "current": None,
           "video_torrent": {"title": "Filme Exemplo 1080p", "seeders": 5,
                             "size": 100, "score": 28.0, "edition": None},
           "audio_torrent": {"title": "Filme Teste antigo", "seeders": 2,
                             "size": 50, "score": 1.0, "edition": None}}
    out = store._legacy_candidates(doc)
    assert candidates.active(out, "video")["id"] == "v0"
    a = candidates.active(out, "audio")
    assert a["title"] == "Filme Teste antigo" and a["magnet"] is None
    assert candidates.selectable(out, "audio") == []


def test_le_serie_no_formato_antigo_e_resolve_ids_antigos(temp_db):
    pack = {"title": "Serie Exemplo S01 1080p", "tracker": "t", "seeders": 3,
            "size": 900, "quality": "1080p", "coverage": "S01 completa",
            "score": 28.0, "tier": 28, "magnet": "magnet:?xt=urn:btih:" + "a" * 40,
            "link": None, "infohash": None, "files": None}
    avulso = dict(pack, title="Serie Exemplo S01E02 720p", coverage="S01E02",
                  magnet="magnet:?xt=urn:btih:" + "b" * 40)
    doc = {"id": "old3", "media_type": "tv", "status": "awaiting",
           "created_at": "2026-01-01T00:00:00", "mode": "manual",
           "episodes": {}, "torrents": [],
           "awaiting": {"reason": "manual_pick", "payload": {"by_torrent": "velho"}},
           "search_tv": {"original": {
               "S01E01": [dict(pack, id="S01E01:o0")],
               "S01E02": [dict(avulso, id="S01E02:o0"), dict(pack, id="S01E02:o1")],
           }, "dubbed": {}}}
    _grava_doc_antigo(doc)
    job = store.get_job("old3")
    rows = candidates.of_role(job, "original")
    assert len(rows) == 2                         # 3 cópias, 2 torrents
    assert job["awaiting"] == {"reason": "manual_pick", "payload": None}
    # o id do primeiro vale; o dos outros episódios resolve pela posição
    p = candidates.find(job, "S01E01:o0", "original")
    assert p["title"] == pack["title"]
    assert candidates.find(job, "S01E02:o1", "original") is p
    assert candidates.find(job, "S01E02:o0", "original")["title"] == avulso["title"]
    assert candidates.find(job, "S01E02:o5", "original") is None


def test_eventos_antigos_com_rastro_inline(temp_db):
    v0 = _slim_antigo("v0", "Filme Exemplo 2160p")
    doc = {"id": "old4", "status": "done", "created_at": "2026-01-01T00:00:00",
           "search": {"audio": [], "video": [v0]}, "fallbacks": None,
           "current": {"video": v0}, "video_torrent": None, "audio_torrent": None}
    _grava_doc_antigo(doc)
    rastro = [
        {"title": "Filme Exemplo 2160p", "tracker": "t1", "seeders": 5, "size": 100,
         "edition": None, "quality": "2160p", "score": 28.0, "rejected": None,
         "chosen": False, "year_match": False},
        {"title": "Filme Exemplo CAM", "tracker": "t1", "seeders": 9, "size": 10,
         "edition": None, "quality": "CAM", "score": None,
         "rejected": "qualidade muito baixa (CAM/TS)", "chosen": False,
         "year_match": False},
    ]
    store.add_event("old4", {"ts": "2026-01-01T00:00:01", "kind": "candidates",
                             "message": "Avaliação para VÍDEO",
                             "data": {"role": "video", "query": "Filme Exemplo 2014",
                                      "candidates": rastro}})
    # antes da migração: o evento antigo passa como está
    detail = jobs.get_job("old4")
    ev = next(e for e in detail["events"] if e["kind"] == "candidates")
    assert ev["data"]["candidates"] == rastro

    # migração: o evento passa a referenciar a consulta e a lupa mostra igual
    assert store.migrate_candidate_events("old4") == 1
    with store._lock:
        raw = json.loads(store._conn.execute(
            "SELECT data FROM events WHERE job_id = 'old4' AND kind = 'candidates'"
        ).fetchone()[0])
    assert "candidates" not in raw and raw["search_query"] == 0
    detail = jobs.get_job("old4")
    ev = next(e for e in detail["events"] if e["kind"] == "candidates")
    assert ev["data"]["candidates"] == rastro
    job = store.get_job("old4")
    # o viável do rastro é a MESMA linha da lista escolhível (v0)
    assert len(job["search_candidates"]) == 2
    assert candidates.find(job, "v0")["trace"] == 0
    assert detail["search"]["video"][0]["id"] == "v0"
    # rodar de novo não duplica
    assert store.migrate_candidate_events("old4") == 0
