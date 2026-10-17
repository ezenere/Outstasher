"""EDLs do alinhador em tabelas (edls / edl_segments) em vez de dentro do
documento do job (série: job["episodes"][k]["edl"], antes na coluna
provisória job_episodes.edl; filme: job["advanced"]["edl"]).

Cobre: round-trip exato (EDL do alinhador e EDL montada à mão, com chaves
fora do padrão), o perfil de confiança em BLOB float32 sem perda, hash por
EDL (estado do episódio não regrava a EDL; a revisão de um episódio regrava
só a dele), listagens sem EDL (e o upsert de um dict assim não apaga nada),
job apagado levando as linhas, o payload derivado do gate de revisão do
filme e a leitura dos formatos antigos (EDL no documento, na coluna
provisória e a cópia gravada no payload do gate).
"""
import json

import numpy as np

from services import jobs, store
from services.jobs import views
from services.series.align import classify, edl as edl_mod


def _segs(dur: float) -> list[classify.Segment]:
    """Segmentos como o alinhador os entrega: refino, junção da cena cortada
    e decisão de revisão no extra."""
    out, t, i = [], 0.0, 0
    while t < dur:
        a1 = min(dur, t + 37.3 + (i % 7) * 11.1)
        kind = ("match", "replaced", "gap_dub", "pal", "drift")[i % 5]
        s = classify.Segment(
            kind=kind, a_start=t, a_end=a1,
            b_start=None if kind == "gap_dub" else t + 1.234567,
            b_end=None if kind == "gap_dub" else a1 + 1.234567,
            slope=None if kind == "gap_dub" else 1.0 + i * 1e-5,
            residual=3.14159 * (i % 4), confidence=0.5 + (i % 50) / 100.0,
            offset=None if kind == "gap_dub" else 1.234567,
            note="revisar" if kind == "replaced" else "")
        if i % 3 == 0:
            s.extra["refine"] = f"offset medido {i * 1.5:+.1f} ms"
            s.extra["video_offset"] = 1.2345678
        if kind == "replaced":
            s.extra["junction_a"] = round(t + 2.5, 3)
            s.extra["action"] = "fill_original" if i % 2 else None
        out.append(s)
        t, i = a1, i + 1
    return out


def _edl_real(key="S01E01", dur=2513.7, fundido=False) -> dict:
    segs = _segs(dur)
    out = edl_mod.build(segs, key, f"/dl/d/{key}.mkv", dur, f"/dl/o/{key}.mkv",
                        dur + 0.5, profile=classify.confidence_profile(segs, dur))
    if fundido:
        out["merged_side"] = "orig"
        out["b_window"] = [12.345, 2600.125]
        out["note"] = "arquivo original fundido (razão 1.98): episódio localizado"
    return out


def _edl_a_mao() -> dict:
    """EDL fora do padrão (jobs antigos, testes): chaves desconhecidas,
    chaves padrão ausentes, int onde o alinhador põe float, None em chave
    opcional, segmento que nem é dict."""
    return {"version": 3, "episode": None,
            "source_dub": {"path": "/d.mkv", "duration": 100},     # int
            "segments": [{"kind": "match", "a0": 0.0, "a1": 10.5, "offset": -1.25},
                         {"kind": "replaced", "a_start": 1, "a_end": 2.0,
                          "b_start": None, "b_end": None, "slope": None,
                          "residual": 0.0, "confidence": True, "offset": None,
                          "note": None, "action": None, "extra": {}},
                         "lixo"],
            "confidence_profile": [0.9, 0.85, 1],                  # int no fim
            "review": {"required": False, "flagged": [], "quem": "regra"},
            "note": None, "a_window": [1.0, None], "pista_nova": {"x": [1, 2]}}


def _ep(s, e, state="pending", **kw):
    ep = {"season": s, "episode": e, "name": f"Episódio {e}",
          "air_date": "2020-01-01", "runtime": 40, "state": state,
          "src": {}, "output": None, "error": None}
    ep.update(kw)
    return ep


def _serie(jid="tve", n_eps=6) -> dict:
    eps = {f"S01E{e:02d}": _ep(1, e) for e in range(1, n_eps + 1)}
    for k in list(eps)[:4]:
        eps[k].update(state="review", edl=_edl_real(k))
    return {"id": jid, "media_type": "tv", "tmdb_id": 9, "language": "pt",
            "mode": "auto", "kind": "both", "status": "awaiting", "detail": "",
            "output": None, "progress": {}, "created_at": "2026-01-01T00:00:00",
            "movie": None, "order_map": None, "episodes": eps, "torrents": [],
            "awaiting": {"reason": "alignment_review", "payload": None},
            "search_queries": [], "search_candidates": []}


def _filme(jid="mve") -> dict:
    return {"id": jid, "tmdb_id": 5, "language": "pt", "mode": "files",
            "kind": "both", "status": "awaiting", "detail": "",
            "created_at": "2026-01-01T00:00:00", "output": None,
            "progress": {"video": None, "audio": None},
            "advanced": {"video_file": "/dl/o/filme.mkv",
                         "audio_file": "/dl/d/filme.mkv",
                         "edl": _edl_real("filme", dur=7203.2)},
            "awaiting": {"reason": "alignment_review", "payload": None},
            "search_queries": [], "search_candidates": []}


def _data(jid) -> dict:
    with store._lock:
        return json.loads(store._conn.execute(
            "SELECT data FROM jobs WHERE id = ?", (jid,)).fetchone()[0])


def _col(jid, col):
    with store._lock:
        return store._conn.execute(
            f"SELECT {col} FROM jobs WHERE id = ?", (jid,)).fetchone()[0]


def _count(table, jid) -> int:
    with store._lock:
        return store._conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE job_id = ?", (jid,)).fetchone()[0]


def _insert_doc(doc: dict):
    """Grava o documento cru em jobs.data (como um processo antigo deixou)."""
    with store._lock:
        store._conn.execute(
            "INSERT INTO jobs (id, created_at, status, data) VALUES (?, ?, ?, ?)",
            (doc["id"], doc["created_at"], doc["status"], json.dumps(doc)))
        store._conn.commit()


class _Sql:
    """Captura os comandos que o SQLite executa durante o bloco."""

    def __enter__(self):
        self.stmts: list[str] = []
        store._conn.set_trace_callback(self.stmts.append)
        return self

    def __exit__(self, *exc):
        store._conn.set_trace_callback(None)

    def touching(self, table, verbs=("INSERT", "UPDATE", "DELETE")) -> list[str]:
        return [s for s in self.stmts if s.lstrip().upper().startswith(verbs)
                and f" {table} " in f" {s} ".replace("(", " ")]


# -------------------- round-trip --------------------

def test_round_trip_edl_do_alinhador(temp_db):
    job = _serie()
    job["episodes"]["S01E02"]["edl"] = _edl_real("S01E02", fundido=True)
    esperado = json.loads(json.dumps(job["episodes"]))
    store.upsert_job(job)
    back = store.get_job(job["id"])
    assert back["episodes"] == esperado
    # o formato do alinhador cabe todo nas colunas: nada vai para o spill
    with store._lock:
        rows = store._conn.execute(
            "SELECT spill, length(confidence_profile), profile_digits "
            "FROM edls WHERE job_id = ?", (job["id"],)).fetchall()
        seg_spill = store._conn.execute(
            "SELECT COUNT(*) FROM edl_segments WHERE job_id = ? "
            "AND spill IS NOT NULL", (job["id"],)).fetchone()[0]
    assert len(rows) == 4 and seg_spill == 0
    assert all(sp is None and n == 4 * 2514 and d == 3 for sp, n, d in rows)
    # nem a EDL nem a coluna provisória carregam nada no documento/episódio
    assert "episodes" not in _data(job["id"])
    with store._lock:
        assert store._conn.execute(
            "SELECT COUNT(*) FROM job_episodes WHERE job_id = ? AND edl IS NOT NULL",
            (job["id"],)).fetchone()[0] == 0
    # a EDL relida volta aos Segments iguais (o que o render consome)
    a = edl_mod.segments(job["episodes"]["S01E01"]["edl"])
    b = edl_mod.segments(back["episodes"]["S01E01"]["edl"])
    assert a == b


def test_round_trip_edl_fora_do_padrao():
    edl = _edl_a_mao()
    back = store._edl_round_trip(edl)
    assert back == edl
    assert type(back["source_dub"]["duration"]) is int
    assert type(back["confidence_profile"][2]) is int
    assert type(back["segments"][1]["a_start"]) is int
    assert back["segments"][1]["confidence"] is True
    assert "action" in back["segments"][1] and back["segments"][1]["action"] is None
    assert "source_orig" not in back and "merged_side" not in back
    # e a do alinhador, com as chaves opcionais do arquivo fundido
    real = _edl_real(fundido=True)
    assert store._edl_round_trip(real) == real


def test_perfil_float32_sem_perda():
    # todos os valores de 3 casas de [0, 1] (os que o alinhador gera)
    vals = [round(k / 1000, 3) for k in range(1001)]
    blob, digits = store._profile_blob(tuple(vals))
    assert digits == 3 and len(blob) == 4 * len(vals)
    assert store._profile_from_blob(blob, digits) == vals
    # o que float32 + 3 casas não devolve igual vai em float64, sem perda
    for vals in ([0.12345, 0.5], [200000.123], [1e300]):
        blob, digits = store._profile_blob(tuple(vals))
        assert digits is None and store._profile_from_blob(blob, None) == vals
    vals = [float("inf"), 0.1]                   # inf passa igual em float32
    assert store._profile_from_blob(*store._profile_blob(tuple(vals))) == vals
    # perfil vazio continua lista vazia (não vira None)
    assert store._edl_round_trip({"confidence_profile": []}) == {
        "confidence_profile": []}


def test_perfil_vazio_e_nulo_no_banco(temp_db):
    job = _serie(n_eps=2)
    job["episodes"]["S01E01"]["edl"]["confidence_profile"] = []
    job["episodes"]["S01E02"]["edl"] = dict(_edl_real("S01E02"),
                                            confidence_profile=None)
    store.upsert_job(job)
    back = store.get_job(job["id"])["episodes"]
    assert back["S01E01"]["edl"]["confidence_profile"] == []
    assert back["S01E02"]["edl"]["confidence_profile"] is None


def test_filme_edl_em_tabela_e_payload_derivado(temp_db):
    job = _filme()
    store.upsert_job(job)
    data = _data(job["id"])
    # os dois arquivos e o motivo do gate em colunas; a EDL e o payload não
    # vão para lugar nenhum do documento
    assert "advanced" not in data and "awaiting" not in data
    assert (_col(job["id"], "adv_video_file"), _col(job["id"], "adv_audio_file")) \
        == ("/dl/o/filme.mkv", "/dl/d/filme.mkv")
    assert _col(job["id"], "awaiting_reason") == "alignment_review"
    assert len(json.dumps(data)) < 1_000
    back = store.get_job(job["id"])
    assert back["advanced"] == json.loads(json.dumps(job["advanced"]))
    assert list(back["advanced"]) == ["video_file", "audio_file", "edl"]
    # a UI recebe o mesmo JSON de antes: a EDL no payload do gate
    pub = jobs._public(back)
    assert pub["awaiting"]["payload"] == {"episodes": {"filme": back["advanced"]["edl"]}}
    # gate resolvido / advanced descartado (troca de torrent): a EDL sai
    job.pop("advanced")
    job["awaiting"] = None
    store.upsert_job(job)
    assert _count("edls", job["id"]) == 0 and _count("edl_segments", job["id"]) == 0


def test_job_apagado_leva_as_edls(temp_db):
    for job in (_serie(), _filme()):
        store.upsert_job(job)
        assert _count("edls", job["id"]) and _count("edl_segments", job["id"])
        store.delete_job(job["id"])
        assert _count("edls", job["id"]) == 0
        assert _count("edl_segments", job["id"]) == 0


# -------------------- hash por EDL --------------------

def test_estado_do_episodio_nao_regrava_a_edl(temp_db):
    job = _serie()
    store.upsert_job(job)

    # o merge conclui um episódio: só a linha dele em job_episodes
    job["episodes"]["S01E01"].update(state="done", output="/dest/S01E01.mkv")
    with _Sql() as sql:
        store.upsert_job(job, create=False)
    assert sql.touching("edls") == [] and sql.touching("edl_segments") == []
    assert len(sql.touching("job_episodes")) == 1

    # a revisão decide um segmento de UM episódio: só a EDL dele
    job["episodes"]["S01E02"]["edl"]["segments"][1]["action"] = "silence"
    job["episodes"]["S01E02"]["edl"]["review"]["required"] = False
    with _Sql() as sql:
        store.upsert_job(job, create=False)
    escritos = sql.touching("edls") + sql.touching("edl_segments")
    assert escritos and all("'S01E02'" in s for s in escritos)
    assert sql.touching("job_episodes") == []
    back = store.get_job(job["id"])["episodes"]["S01E02"]["edl"]
    assert back["segments"][1]["action"] == "silence"
    assert back["review"]["required"] is False

    # rematch religa o dublado e descarta a EDL; episódio removido leva a dele
    job["episodes"]["S01E03"].pop("edl")
    del job["episodes"]["S01E04"]
    store.upsert_job(job, create=False)
    with store._lock:
        keys = {r[0] for r in store._conn.execute(
            "SELECT ep_key FROM edls WHERE job_id = ?", (job["id"],))}
    assert keys == {"S01E01", "S01E02"}
    assert {r for r in store._conn.execute(
        "SELECT DISTINCT ep_key FROM edl_segments WHERE job_id = ?",
        (job["id"],)).fetchall()} == {("S01E01",), ("S01E02",)}


def test_restart_reescreve_as_edls_uma_vez(temp_db):
    job = _serie()
    store.upsert_job(job)
    store._section_hash.clear()                  # "restart"
    job = store.get_job(job["id"])
    with _Sql() as sql:
        store.upsert_job(job, create=False)
    assert len(sql.touching("edls", ("INSERT",))) == 4
    with _Sql() as sql:
        store.upsert_job(job, create=False)
    assert sql.touching("edls") == [] and sql.touching("edl_segments") == []


# -------------------- listagens sem EDL --------------------

def test_listagem_nao_carrega_edls(temp_db):
    serie, filme = _serie(), _filme()
    for job in (serie, filme):
        job["status"] = "error"
        store.upsert_job(job)
    with _Sql() as sql:
        rows = store.load_jobs_by_status(("error",), sections=store.LIST_SECTIONS)
        views.list_group("error")
        views.list_group("all", per_page=10)
        views.summary()
        views.list_jobs()
    assert not [s for s in sql.stmts if " edls " in f" {s} " or "edl_segments" in s]
    assert not any("edl, " in s or ", edl " in s for s in sql.stmts
                   if "job_episodes" in s)
    by_id = {j["id"]: j for j in rows}
    assert all("edl" not in ep for ep in by_id["tve"]["episodes"].values())
    assert by_id["mve"]["advanced"] == {"video_file": "/dl/o/filme.mkv",
                                        "audio_file": "/dl/d/filme.mkv"}
    # a marca não vaza para a API
    assert store.EDLS_UNLOADED not in jobs._public(by_id["tve"])

    # um dict assim, regravado, não apaga EDL nenhuma
    for j in rows:
        store.upsert_job(j, create=False)
    assert _count("edls", "tve") == 4 and _count("edls", "mve") == 1
    assert store.get_job("tve")["episodes"] == \
        json.loads(json.dumps(serie["episodes"]))
    assert store.EDLS_UNLOADED not in _data("tve")

    # o detalhe (todas as seções) traz as EDLs
    assert store.get_job("mve")["advanced"]["edl"] == \
        json.loads(json.dumps(filme["advanced"]["edl"]))


# -------------------- formatos antigos --------------------

def test_legacy_edls_funcao_pura():
    doc = json.loads(json.dumps(_filme()))
    doc["awaiting"]["payload"] = {"episodes": {"filme": doc["advanced"]["edl"]}}
    copia = json.loads(json.dumps(doc))
    assert store._has_legacy_edls(doc)
    out = store._legacy_edls(doc)
    assert doc == copia                          # não muta a entrada
    assert out["awaiting"] == {"reason": "alignment_review", "payload": None}
    assert out["advanced"] == doc["advanced"]
    assert not store._has_legacy_edls(
        {"media_type": "tv", "awaiting": {"reason": "alignment_review",
                                          "payload": None}})


def test_le_filme_antigo_com_edl_no_documento(temp_db):
    doc = json.loads(json.dumps(_filme()))
    doc["awaiting"]["payload"] = {"episodes": {"filme": doc["advanced"]["edl"]}}
    _insert_doc(doc)
    job = store.get_job(doc["id"])
    assert job["advanced"] == doc["advanced"]
    assert job["awaiting"]["payload"] is None
    assert jobs._public(job)["awaiting"]["payload"] == {
        "episodes": {"filme": doc["advanced"]["edl"]}}
    # listagem do mesmo documento antigo: sem a EDL
    [j] = store.load_jobs_by_status(("awaiting",), sections=store.LIST_SECTIONS)
    assert "edl" not in j["advanced"]
    # job ativo regravado sai no formato novo, e volta igual
    store.upsert_job(job)
    assert "advanced" not in _data(doc["id"]) and "awaiting" not in _data(doc["id"])
    assert store.get_job(doc["id"])["awaiting"] == {"reason": "alignment_review",
                                                    "payload": None}
    assert store.get_job(doc["id"])["advanced"] == doc["advanced"]


def test_le_serie_antiga_com_edl_no_documento(temp_db):
    job = _serie()
    doc = {k: v for k, v in job.items()
           if k not in ("search_queries", "search_candidates")}
    doc["search_tv"] = {"original": {}, "dubbed": {}}
    doc["awaiting"] = {"reason": "alignment_review", "payload": {"episodes": {
        k: ep["edl"] for k, ep in job["episodes"].items() if "edl" in ep}}}
    doc = json.loads(json.dumps(doc))
    _insert_doc(doc)
    back = store.get_job(doc["id"])
    assert back["episodes"] == doc["episodes"]
    assert back["awaiting"]["payload"] is None
    pub = jobs._public(back)
    assert pub["awaiting"]["payload"] == doc["awaiting"]["payload"]
    store.upsert_job(back)
    assert _count("edls", doc["id"]) == 4
    assert "awaiting" not in _data(doc["id"])
    assert store.get_job(doc["id"])["awaiting"] == {"reason": "alignment_review",
                                                    "payload": None}
    assert store.get_job(doc["id"])["episodes"] == doc["episodes"]


def test_le_edl_da_coluna_provisoria(temp_db):
    """Banco gravado pelo passo anterior: a EDL do episódio em JSON na coluna
    job_episodes.edl, sem linha em edls."""
    job = _serie()
    store.upsert_job(job)
    edls = {k: ep["edl"] for k, ep in job["episodes"].items() if "edl" in ep}
    with store._lock:
        store._conn.execute("DELETE FROM edls WHERE job_id = ?", (job["id"],))
        store._conn.executemany(
            "UPDATE job_episodes SET edl = ? WHERE job_id = ? AND ep_key = ?",
            [(json.dumps(e), job["id"], k) for k, e in edls.items()])
        store._conn.commit()
    store._section_hash.clear()                  # "restart"
    esperado = json.loads(json.dumps(job["episodes"]))

    # listagem não lê a coluna; e regravar um dict de listagem não a perde
    [j] = store.load_jobs_by_status(("awaiting",), sections=store.LIST_SECTIONS)
    assert all("edl" not in ep for ep in j["episodes"].values())
    store.upsert_job(j, create=False)
    assert store.get_job(job["id"])["episodes"] == esperado

    # o dict completo regravado muda a EDL para as tabelas e zera a coluna —
    # inclusive a da EDL que o dict descartou (rematch), que não pode voltar
    full = store.get_job(job["id"])
    full["episodes"]["S01E04"].pop("edl")
    del esperado["S01E04"]["edl"]
    store.upsert_job(full, create=False)
    assert _count("edls", job["id"]) == 3
    with store._lock:
        assert store._conn.execute(
            "SELECT COUNT(*) FROM job_episodes WHERE job_id = ? AND edl IS NOT NULL",
            (job["id"],)).fetchone()[0] == 0
    assert store.get_job(job["id"])["episodes"] == esperado


# -------------------- tamanho --------------------

def test_edl_tipica_em_bytes(temp_db):
    """O perfil (o grosso da EDL em JSON) vira 4 bytes por segundo."""
    edl = _edl_real()
    as_json = len(json.dumps(edl, ensure_ascii=False))
    prof_json = len(json.dumps(edl["confidence_profile"]))
    blob, _ = store._profile_blob(tuple(edl["confidence_profile"]))
    assert len(blob) == 4 * len(edl["confidence_profile"]) < prof_json
    assert np.frombuffer(blob, "<f4").dtype == np.float32
    assert as_json > len(blob)


def test_edl_sem_linhas_montadas_grava_no_proximo_upsert(temp_db):
    """As linhas só são montadas (fora do lock) para as EDLs cujo hash difere
    do cache na hora do preparo. Se outra gravação do mesmo job mexer no cache
    entre o preparo e a escrita, a EDL que "mudou" sem linhas fica como está
    e sem hash — o próximo upsert a grava."""
    job = _serie()
    store.upsert_job(job)
    job["episodes"]["S01E01"]["edl"]["segments"][1]["action"] = "silence"
    prepared = store._prepare_upsert(job)
    assert prepared["edls"]["S01E01"][1] is not None
    assert prepared["edls"]["S01E02"][1] is None          # inalterada: só o hash
    store._section_hash[job["id"]]["edls"]["S01E02"] = "outra gravação"
    with store._lock:
        assert store._apply_upsert(job, prepared, create=False)
        store._conn.commit()
        store._remember(job["id"], prepared)
    assert prepared["edls_missed"] == ["S01E02"]
    assert "S01E02" not in store._section_hash[job["id"]]["edls"]
    assert _count("edls", job["id"]) == 4                 # nada se perdeu
    with _Sql() as sql:
        store.upsert_job(job, create=False)
    assert sql.touching("edls") and all("'S01E02'" in s for s in sql.touching("edls"))
    assert store.get_job(job["id"])["episodes"] == json.loads(json.dumps(job["episodes"]))


def test_payload_de_revisao_nunca_vai_para_o_documento(temp_db):
    """Dict que ainda traz a cópia das EDLs no gate (jobs.json antigo
    importado direto, teste montado à mão): o documento grava só o motivo."""
    job = _filme()
    job["awaiting"]["payload"] = {"episodes": {"filme": job["advanced"]["edl"]}}
    store.upsert_job(job)
    assert "awaiting" not in _data(job["id"])
    assert _col(job["id"], "awaiting_reason") == "alignment_review"
    assert store.get_job(job["id"])["awaiting"] == {"reason": "alignment_review",
                                                    "payload": None}
    assert job["awaiting"]["payload"] is not None          # o dict não é mexido
