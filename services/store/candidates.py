"""Candidatos de busca do job.

Tabelas search_queries / search_candidates / candidate_episodes, a conversão
das linhas (gravação e leitura) e as normalizações do formato antigo: as
listas `search` / `fallbacks` / `current` / `search_tv` dentro do documento
(_legacy_candidates) e o rastro inteiro em events.data
(_legacy_candidate_events).
"""
import hashlib
import json

from services import candidates as cands
from services import store


# -------------------- candidatos de busca (tabelas do job) --------------------
# Formato em memória e semântica: services/candidates.py. Uma linha por
# candidato (o rastro de cada consulta, a lista escolhível, as reservas e o
# ativo são colunas da MESMA linha) e a posição de cada um no ranking de cada
# episódio (série) em candidate_episodes.

def _create_candidate_tables():
    store._conn.execute("""
        CREATE TABLE IF NOT EXISTS search_queries (
            job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
            n INTEGER NOT NULL,
            role TEXT NOT NULL,
            query TEXT NOT NULL,
            ts TEXT,
            results INTEGER,
            PRIMARY KEY (job_id, n)
        )""")
    store._conn.execute("""
        CREATE TABLE IF NOT EXISTS search_candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
            cid TEXT NOT NULL,
            query_n INTEGER,
            role TEXT NOT NULL,
            title TEXT NOT NULL,
            tracker TEXT,
            seeders INTEGER,
            size INTEGER,
            edition TEXT,
            quality TEXT,
            coverage_label TEXT,
            score REAL,
            tier INTEGER,
            year_match INTEGER,
            rejected TEXT,
            chosen INTEGER,
            magnet TEXT,
            link TEXT,
            infohash TEXT,
            files INTEGER,
            trace_rank INTEGER,
            rank INTEGER,
            fallback INTEGER,
            active INTEGER NOT NULL DEFAULT 0,
            UNIQUE (job_id, cid),
            FOREIGN KEY (job_id, query_n)
                REFERENCES search_queries(job_id, n) ON DELETE CASCADE
        )""")
    store._conn.execute("CREATE INDEX IF NOT EXISTS idx_search_candidates_query "
                  "ON search_candidates(job_id, query_n)")
    store._conn.execute("""
        CREATE TABLE IF NOT EXISTS candidate_episodes (
            candidate_id INTEGER NOT NULL
                REFERENCES search_candidates(id) ON DELETE CASCADE,
            ep_key TEXT NOT NULL,
            rank INTEGER NOT NULL,
            PRIMARY KEY (candidate_id, ep_key)
        )""")


# colunas de search_candidates <-> chaves do candidato em memória
_CAND_COLS = (("cid", "id"), ("query_n", "query"), ("role", "role"),
              ("title", "title"), ("tracker", "tracker"), ("seeders", "seeders"),
              ("size", "size"), ("edition", "edition"), ("quality", "quality"),
              ("coverage_label", "coverage"), ("score", "score"), ("tier", "tier"),
              ("year_match", "year_match"), ("rejected", "rejected"),
              ("chosen", "chosen"), ("magnet", "magnet"), ("link", "link"),
              ("infohash", "infohash"), ("files", "files"),
              ("trace_rank", "trace"), ("rank", "rank"), ("fallback", "fallback"),
              ("active", "active"))
_BOOL_KEYS = {"year_match", "chosen", "active"}

# seções do job que moram em tabela própria (não vão para jobs.data)
_SECTIONS = ("search_queries", "search_candidates")
# chaves do formato antigo: hoje são derivadas/normalizadas — nunca gravadas
_LEGACY_KEYS = ("search", "fallbacks", "current", "video_torrent",
                "audio_torrent", "search_tv")


def _section_rows(job: dict) -> tuple[list, list]:
    """As linhas das tabelas de candidatos, como tuplas prontas para o INSERT.
    Montadas FORA do lock e antes de qualquer escrita: dict mexido por outra
    thread no meio (RuntimeError) não deixa gravação pela metade."""
    qs = [(q["n"], q["role"], q["query"], q.get("ts"), q.get("results"))
          for q in job.get("search_queries") or []]
    cs = []
    for c in job.get("search_candidates") or []:
        vals = tuple(
            (1 if c.get(k) else 0) if k == "active"
            else (None if c.get(k) is None else int(c[k])) if k in _BOOL_KEYS
            else c.get(k)
            for _, k in _CAND_COLS)
        cs.append((vals, tuple((c.get("episodes") or {}).items())))
    return qs, cs


def _rows_digest(rows: tuple[list, list]) -> str:
    raw = json.dumps(rows, ensure_ascii=False)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _write_sections(job_id: str, rows: tuple[list, list]):
    """Reescreve as linhas de candidatos do job (chamado com _lock, dentro da
    transação do upsert)."""
    qs, cs = rows
    # candidatos primeiro (o CASCADE leva candidate_episodes junto); as
    # consultas depois, já sem filhos apontando para elas
    store._conn.execute("DELETE FROM search_candidates WHERE job_id = ?", (job_id,))
    store._conn.execute("DELETE FROM search_queries WHERE job_id = ?", (job_id,))
    store._conn.executemany(
        "INSERT INTO search_queries (job_id, n, role, query, ts, results) "
        "VALUES (?, ?, ?, ?, ?, ?)", [(job_id,) + q for q in qs])
    cols = ", ".join(c for c, _ in _CAND_COLS)
    ph = ", ".join("?" for _ in range(len(_CAND_COLS) + 1))
    for vals, eps in cs:
        cur = store._conn.execute(
            f"INSERT INTO search_candidates (job_id, {cols}) VALUES ({ph})",
            (job_id,) + vals)
        if eps:
            store._conn.executemany(
                "INSERT INTO candidate_episodes (candidate_id, ep_key, rank) "
                "VALUES (?, ?, ?)", [(cur.lastrowid, k, pos) for k, pos in eps])


def _load_candidates(job_ids: list[str]) -> dict[str, dict]:
    """{job_id: {search_queries, search_candidates}} lidos das tabelas (com
    _lock já tomado). Em lotes, para a lista de jobs não virar N consultas."""
    out = {jid: {"search_queries": [], "search_candidates": []} for jid in job_ids}
    cols = ", ".join(c for c, _ in _CAND_COLS)
    for i in range(0, len(job_ids), 500):
        chunk = job_ids[i:i + 500]
        ph = ",".join("?" for _ in chunk)
        for jid, n, role, query, ts, results in store._conn.execute(
                f"SELECT job_id, n, role, query, ts, results FROM search_queries "
                f"WHERE job_id IN ({ph}) ORDER BY job_id, n", chunk):
            out[jid]["search_queries"].append(
                {"n": n, "role": role, "query": query, "ts": ts, "results": results})
        by_rowid: dict[int, dict] = {}
        for r in store._conn.execute(
                f"SELECT id, job_id, {cols} FROM search_candidates "
                f"WHERE job_id IN ({ph}) ORDER BY id", chunk):
            kw = {}
            for (_, k), v in zip(_CAND_COLS, r[2:]):
                kw[k] = bool(v) if (k in _BOOL_KEYS and v is not None) else v
            c = cands.new(kw["role"], kw["id"], kw, **{
                k: kw[k] for k in ("query", "trace", "rank", "fallback")})
            c["active"] = bool(kw["active"])
            by_rowid[r[0]] = c
            out[r[1]]["search_candidates"].append(c)
        for cand_id, key, pos in store._conn.execute(
                f"SELECT e.candidate_id, e.ep_key, e.rank FROM candidate_episodes e "
                f"JOIN search_candidates c ON c.id = e.candidate_id "
                f"WHERE c.job_id IN ({ph}) ORDER BY e.rowid", chunk):
            by_rowid[cand_id]["episodes"][key] = pos
    return out


def _is_legacy(doc: dict) -> bool:
    return any(k in doc for k in _LEGACY_KEYS)


def _legacy_candidates(doc: dict) -> dict:
    """Documento no formato antigo -> formato normalizado (função pura).

    - filme: `search` vira a lista escolhível; `fallbacks` (todas também em
      `search`) vira a posição de reserva; `current` vira o candidato ativo;
      `video_torrent`/`audio_torrent` (cópia do escolhido) só servem para
      achar o ativo de jobs anteriores ao `current` — ou viram um candidato
      sem link, para o detalhe continuar mostrando o que foi baixado.
    - série: `search_tv` (o mesmo pack repetido em cada episódio) vira um
      candidato único com a posição em cada episódio; o id do primeiro vale,
      e os ids antigos dos outros episódios resolvem pela posição
      (candidates.find).
    - gates de série agora derivados: o payload gravado sai.
    """
    doc = dict(doc)
    search = doc.pop("search", None) or {}
    fallbacks = doc.pop("fallbacks", None) or {}
    current = doc.pop("current", None) or {}
    chosen = {"video": doc.pop("video_torrent", None),
              "audio": doc.pop("audio_torrent", None)}
    search_tv = doc.pop("search_tv", None) or {}
    job = {"search_queries": list(doc.get("search_queries") or []),
           "search_candidates": list(doc.get("search_candidates") or [])}

    for role in ("audio", "video"):
        for i, c in enumerate(search.get(role) or []):
            cid = c.get("id") or f"{cands.ROLE_LETTER[role]}{i}"
            if cands.find(job, cid, role) is None:
                cands.rows(job).append(cands.new(role, cid, c, rank=i))
        reservas = [cands.ensure(job, role, c) for c in fallbacks.get(role) or []]
        cands.set_fallbacks(job, role, reservas)
        cur = current.get(role)
        if not cur and chosen[role]:
            # jobs antigos (sem "current"): acha o candidato pelo título escolhido
            title = chosen[role].get("title")
            cur = next((c for c in cands.selectable(job, role)
                        if c["title"] == title), None) or chosen[role]
        if cur:
            cands.set_active(job, role, cands.ensure(job, role, cur))

    for role in ("original", "dubbed"):
        if search_tv.get(role):
            cands.set_series(job, role, search_tv[role])

    gate = doc.get("awaiting")
    if (doc.get("media_type") == "tv" and isinstance(gate, dict)
            and gate.get("reason") in ("manual_pick", "incompatible_torrents")):
        doc["awaiting"] = {**gate, "payload": None}
    doc.update(job)
    return doc


def _legacy_candidate_events(doc: dict, events: list[dict]
                             ) -> tuple[dict, list[tuple[int, dict]]]:
    """Eventos "candidates" do formato antigo (rastro inteiro em events.data)
    -> uma consulta + linhas de rastro no job, e o evento passa a só
    referenciá-la. Função pura: devolve (doc novo, [(id do evento, data
    nova)]). O candidato viável do rastro que já está na lista escolhível do
    papel (mesmo título/tracker) é a MESMA linha — só ganha a posição no
    rastro; os rejeitados viram linhas só de rastro."""
    job = {"search_queries": [dict(q) for q in doc.get("search_queries") or []],
           "search_candidates": [dict(c, episodes=dict(c.get("episodes") or {}))
                                 for c in doc.get("search_candidates") or []]}
    updates: list[tuple[int, dict]] = []
    for ev in events:
        d = ev.get("data")
        if (ev.get("kind") != "candidates" or not isinstance(d, dict)
                or not isinstance(d.get("candidates"), list)):
            continue
        role = d.get("role") or "?"
        n = cands.add_query(job, role, d.get("query") or "", None)
        job["search_queries"][-1]["ts"] = ev.get("ts")
        for i, t in enumerate(d["candidates"]):
            row = None
            if t.get("rejected") is None:
                row = next((c for c in cands.selectable(job, role)
                            if c["query"] is None and c["trace"] is None
                            and c["title"] == t.get("title")
                            and c["tracker"] == t.get("tracker")), None)
            if row is not None:
                row.update(query=n, trace=i)
                for k in ("quality", "rejected", "chosen", "year_match", "coverage"):
                    if row.get(k) is None and k in t:
                        row[k] = t[k]
            else:
                cands.rows(job).append(cands.new(role, f"q{n}.{i}", t, query=n, trace=i))
        data = {k: v for k, v in d.items() if k != "candidates"}
        data["search_query"] = n
        updates.append((ev["id"], data))
    return {**doc, **job}, updates
