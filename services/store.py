"""Persistencia em SQLite (jobs.db).

- jobs: um documento JSON por job (upsert pequeno a cada mudanca), com as
  listas padronizadas fora dele, em tabelas próprias (ver upsert_job):
  search_queries / search_candidates / candidate_episodes.
- events: append-only, um INSERT por evento — nada de regravar tudo.
- WAL + lock: seguro para o event loop e para a thread do merge.
- Migracao automatica: se existir jobs.json (formato antigo), importa e
  renomeia para jobs.json.bak.
"""
import hashlib
import json
import sqlite3
import threading

import config
from services import candidates as cands

MAX_EVENTS_RETURNED = 500  # o detalhe do job devolve os N eventos mais recentes

_conn: sqlite3.Connection | None = None
_lock = threading.Lock()


def init():
    global _conn
    if _conn is not None:
        return
    # garante o diretorio do banco (ex.: /data montado como volume no Docker)
    config.DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    _conn = sqlite3.connect(config.DB_FILE, check_same_thread=False)
    _conn.execute("PRAGMA journal_mode=WAL")
    _conn.execute("PRAGMA synchronous=NORMAL")
    # o SQLite vem com FKs DESLIGADAS por conexão: sem isto o ON DELETE
    # CASCADE das tabelas filhas do job não roda e remover job deixa órfãos
    _conn.execute("PRAGMA foreign_keys = ON")
    _conn.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            status TEXT NOT NULL,
            data TEXT NOT NULL
        )""")
    _conn.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL,
            ts TEXT NOT NULL,
            kind TEXT NOT NULL,
            message TEXT NOT NULL,
            data TEXT
        )""")
    _conn.execute("CREATE INDEX IF NOT EXISTS idx_events_job ON events(job_id)")
    _conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status)")
    # jobs antigos não têm media_type no JSON: IFNULL trata como filme. O índice
    # usa a MESMA expressão das queries para o SQLite conseguir aproveitá-lo.
    _conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_jobs_media "
        "ON jobs(IFNULL(json_extract(data, '$.media_type'), 'movie'))")
    _conn.execute("""
        CREATE TABLE IF NOT EXISTS destinations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT NOT NULL,
            path TEXT NOT NULL,
            media TEXT NOT NULL DEFAULT 'movie',
            is_default INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )""")
    # migração de bancos anteriores à separação filme/série: a coluna `media`
    # aparece via ALTER (CREATE IF NOT EXISTS não altera tabela existente).
    # Destinos antigos viram 'movie' — era o único uso possível até então.
    cols = {r[1] for r in _conn.execute("PRAGMA table_info(destinations)")}
    if "media" not in cols:
        _conn.execute("ALTER TABLE destinations "
                      "ADD COLUMN media TEXT NOT NULL DEFAULT 'movie'")
    _conn.execute("""
        CREATE TABLE IF NOT EXISTS torrent_targets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT NOT NULL,
            save_path TEXT NOT NULL DEFAULT '',
            local_path TEXT NOT NULL DEFAULT '',
            is_default INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )""")
    # tabela chave/valor (senha, api key, e o que mais precisar guardar)
    _conn.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )""")
    # presets de conversao (ConvertOptions nomeadas). name UNIQUE: salvar com um
    # nome existente sobrescreve, em vez de duplicar a entrada na lista.
    _conn.execute("""
        CREATE TABLE IF NOT EXISTS convert_presets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            options TEXT NOT NULL
        )""")
    # idiomas da faixa dublada (editaveis pela UI). markers_* guardados como JSON.
    _conn.execute("""
        CREATE TABLE IF NOT EXISTS languages (
            code TEXT PRIMARY KEY,
            label TEXT NOT NULL,
            tmdb TEXT NOT NULL,
            markers_strong TEXT NOT NULL DEFAULT '[]',
            markers_weak TEXT NOT NULL DEFAULT '[]',
            sort_order INTEGER NOT NULL DEFAULT 0
        )""")
    _create_candidate_tables()
    _conn.commit()
    _migrate_from_json()
    _seed_default_destination()
    _seed_default_torrent_target()
    _seed_languages()
    load_language_config()


# -------------------- settings (chave/valor) --------------------

def get_setting(key: str, default: str | None = None) -> str | None:
    with _lock:
        r = _conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return r[0] if r else default


def set_setting(key: str, value: str):
    with _lock:
        _conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value))
        _conn.commit()


# -------------------- regras de busca extra (idioma x variante x indexers) --------------------
# Guardadas como um único JSON em settings. Formato:
#   { "<lang>": { "no_year": ["indexerId", ...], "roman": [...], "roman_no_year": [...] } }
# lang = código do idioma (pt/es/...); as chaves de variante são fixas.
_EXTRA_SEARCH_KEY = "extra_search_rules"
EXTRA_SEARCH_VARIANTS = ("no_year", "roman", "roman_no_year")


def get_extra_search_rules() -> dict:
    raw = get_setting(_EXTRA_SEARCH_KEY)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def set_extra_search_rules(rules: dict):
    set_setting(_EXTRA_SEARCH_KEY, json.dumps(rules, ensure_ascii=False))


# -------------------- presets de conversao --------------------
# ConvertOptions nomeadas ("QSV AV1 CRF 20"). Guardadas como JSON opaco: quem
# valida e o transcode.validate() na hora de converter, entao um preset salvo
# antes de uma mudanca de capabilities nao quebra a listagem.

def list_convert_presets() -> list[dict]:
    with _lock:
        rows = _conn.execute(
            "SELECT id, name, options FROM convert_presets "
            "ORDER BY name COLLATE NOCASE").fetchall()
    return [{"id": r[0], "name": r[1], "options": _json_or_none(r[2])} for r in rows]


def _json_or_none(raw: str) -> dict | None:
    try:
        v = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return v if isinstance(v, dict) else None


def create_convert_preset(name: str, options: dict) -> dict:
    """Cria ou sobrescreve pelo nome (salvar de novo com o mesmo nome atualiza)."""
    payload = json.dumps(options, ensure_ascii=False)
    with _lock:
        _conn.execute(
            "INSERT INTO convert_presets (name, options) VALUES (?, ?) "
            "ON CONFLICT(name) DO UPDATE SET options = excluded.options",
            (name, payload))
        _conn.commit()
        r = _conn.execute(
            "SELECT id, name, options FROM convert_presets WHERE name = ?",
            (name,)).fetchone()
    return {"id": r[0], "name": r[1], "options": _json_or_none(r[2])}


def delete_convert_preset(preset_id: int) -> bool:
    with _lock:
        cur = _conn.execute("DELETE FROM convert_presets WHERE id = ?", (preset_id,))
        _conn.commit()
        return cur.rowcount > 0


# -------------------- destinos (pastas de destino do arquivo final) --------------------

def list_destinations() -> list[dict]:
    with _lock:
        rows = _conn.execute(
            "SELECT id, label, path, media, is_default FROM destinations "
            "ORDER BY is_default DESC, label COLLATE NOCASE").fetchall()
    return [_dest_row(r) for r in rows]


def _dest_row(r) -> dict:
    return {"id": r[0], "label": r[1], "path": r[2], "media": r[3],
            "is_default": bool(r[4])}


def list_destinations_by_media(media: str) -> list[dict]:
    """Destinos de UMA mídia ('movie'/'tv') — filmes e séries têm bibliotecas
    separadas; cada fluxo só enxerga os destinos da própria mídia."""
    with _lock:
        rows = _conn.execute(
            "SELECT id, label, path, media, is_default FROM destinations "
            "WHERE media = ? ORDER BY is_default DESC, label COLLATE NOCASE",
            (media,)).fetchall()
    return [_dest_row(r) for r in rows]


def get_destination(dest_id: int) -> dict | None:
    with _lock:
        r = _conn.execute(
            "SELECT id, label, path, media, is_default FROM destinations WHERE id = ?",
            (dest_id,)).fetchone()
    return _dest_row(r) if r else None


def default_destination(media: str = "movie") -> dict | None:
    """Destino padrão DA MÍDIA (o is_default é por mídia)."""
    with _lock:
        r = _conn.execute(
            "SELECT id, label, path, media, is_default FROM destinations "
            "WHERE media = ? ORDER BY is_default DESC, id LIMIT 1",
            (media,)).fetchone()
    return _dest_row(r) if r else None


def add_destination(label: str, path: str, is_default: bool = False,
                    media: str = "movie") -> dict:
    from datetime import datetime
    with _lock:
        if is_default:
            # o padrão é POR mídia: marcar um destino de série como padrão não
            # desmarca o padrão dos filmes
            _conn.execute("UPDATE destinations SET is_default = 0 WHERE media = ?",
                          (media,))
        cur = _conn.execute(
            "INSERT INTO destinations (label, path, media, is_default, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (label, path, media, 1 if is_default else 0,
             datetime.now().isoformat(timespec="seconds")))
        _conn.commit()
        dest_id = cur.lastrowid
    return get_destination(dest_id)


def update_destination(dest_id: int, label: str, path: str, is_default: bool,
                       media: str | None = None) -> dict | None:
    with _lock:
        row = _conn.execute(
            "SELECT media FROM destinations WHERE id = ?", (dest_id,)).fetchone()
        if not row:
            return None
        media = media or row[0]
        if is_default:
            _conn.execute("UPDATE destinations SET is_default = 0 WHERE media = ?",
                          (media,))
        _conn.execute(
            "UPDATE destinations SET label = ?, path = ?, media = ?, "
            "is_default = ? WHERE id = ?",
            (label, path, media, 1 if is_default else 0, dest_id))
        _conn.commit()
    return get_destination(dest_id)


def delete_destination(dest_id: int) -> bool:
    with _lock:
        cur = _conn.execute("DELETE FROM destinations WHERE id = ?", (dest_id,))
        _conn.commit()
        return cur.rowcount > 0


def _seed_default_destination():
    """Na primeira vez, cria um destino a partir do OUTPUT_DIR do .env (se houver)."""
    with _lock:
        count = _conn.execute("SELECT COUNT(*) FROM destinations").fetchone()[0]
    if count:
        return
    output_dir = getattr(config, "OUTPUT_DIR", None)
    if output_dir:
        # as.posix() evita backslashes quando o .env foi lido no Windows
        add_destination("Padrão (OUTPUT_DIR)", output_dir.as_posix(), is_default=True)


# -------------------- destinos dos torrents (qBittorrent) --------------------

def _target_row(r) -> dict:
    return {"id": r[0], "label": r[1], "save_path": r[2], "local_path": r[3],
            "is_default": bool(r[4])}


def list_torrent_targets() -> list[dict]:
    with _lock:
        rows = _conn.execute(
            "SELECT id, label, save_path, local_path, is_default FROM torrent_targets "
            "ORDER BY is_default DESC, label COLLATE NOCASE").fetchall()
    return [_target_row(r) for r in rows]


def get_torrent_target(target_id: int) -> dict | None:
    with _lock:
        r = _conn.execute(
            "SELECT id, label, save_path, local_path, is_default FROM torrent_targets "
            "WHERE id = ?", (target_id,)).fetchone()
    return _target_row(r) if r else None


def default_torrent_target() -> dict | None:
    with _lock:
        r = _conn.execute(
            "SELECT id, label, save_path, local_path, is_default FROM torrent_targets "
            "ORDER BY is_default DESC, id LIMIT 1").fetchone()
    return _target_row(r) if r else None


def add_torrent_target(label: str, save_path: str, local_path: str,
                       is_default: bool = False) -> dict:
    from datetime import datetime
    with _lock:
        if is_default:
            _conn.execute("UPDATE torrent_targets SET is_default = 0")
        cur = _conn.execute(
            "INSERT INTO torrent_targets (label, save_path, local_path, is_default, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (label, save_path, local_path, 1 if is_default else 0,
             datetime.now().isoformat(timespec="seconds")))
        _conn.commit()
        target_id = cur.lastrowid
    return get_torrent_target(target_id)


def update_torrent_target(target_id: int, label: str, save_path: str,
                          local_path: str, is_default: bool) -> dict | None:
    with _lock:
        if not _conn.execute("SELECT 1 FROM torrent_targets WHERE id = ?",
                             (target_id,)).fetchone():
            return None
        if is_default:
            _conn.execute("UPDATE torrent_targets SET is_default = 0")
        _conn.execute(
            "UPDATE torrent_targets SET label = ?, save_path = ?, local_path = ?, "
            "is_default = ? WHERE id = ?",
            (label, save_path, local_path, 1 if is_default else 0, target_id))
        _conn.commit()
    return get_torrent_target(target_id)


def delete_torrent_target(target_id: int) -> bool:
    with _lock:
        cur = _conn.execute("DELETE FROM torrent_targets WHERE id = ?", (target_id,))
        _conn.commit()
        return cur.rowcount > 0


def _seed_default_torrent_target():
    """Na primeira vez, cria um target a partir do QBIT_SAVE_PATH/QBIT_PATH_MAP do .env."""
    with _lock:
        count = _conn.execute("SELECT COUNT(*) FROM torrent_targets").fetchone()[0]
    if count:
        return
    save_path = getattr(config, "QBIT_SAVE_PATH", "") or ""
    path_map = getattr(config, "QBIT_PATH_MAP", []) or []
    # se havia QBIT_PATH_MAP, usa o primeiro par como save->local do target
    local_path = ""
    if path_map:
        src, dst = path_map[0]
        if not save_path:
            save_path = src
        local_path = dst
    if save_path or local_path:
        add_torrent_target("Padrão (.env)", save_path, local_path, is_default=True)


# -------------------- idiomas da faixa dublada (editaveis) --------------------

def _lang_row(r) -> dict:
    return {"code": r[0], "label": r[1], "tmdb": r[2],
            "markers_strong": json.loads(r[3] or "[]"),
            "markers_weak": json.loads(r[4] or "[]"),
            "sort_order": r[5]}


def _seed_languages():
    """Na primeira execução, popula a tabela com os idiomas padrão do config."""
    with _lock:
        count = _conn.execute("SELECT COUNT(*) FROM languages").fetchone()[0]
    if count:
        return
    for i, (code, info) in enumerate(config._DEFAULT_LANGUAGES.items()):
        with _lock:
            _conn.execute(
                "INSERT INTO languages (code, label, tmdb, markers_strong, "
                "markers_weak, sort_order) VALUES (?, ?, ?, ?, ?, ?)",
                (code, info["label"], info["tmdb"],
                 json.dumps(info["markers_strong"], ensure_ascii=False),
                 json.dumps(info["markers_weak"], ensure_ascii=False), i))
            _conn.commit()
    # marcadores de legenda: settings (lista unica, nao por idioma)
    if get_setting("subtitle_markers") is None:
        set_setting("subtitle_markers",
                    json.dumps(config._DEFAULT_SUBTITLE_MARKERS, ensure_ascii=False))


def list_languages() -> list[dict]:
    with _lock:
        rows = _conn.execute(
            "SELECT code, label, tmdb, markers_strong, markers_weak, sort_order "
            "FROM languages ORDER BY sort_order, code").fetchall()
    return [_lang_row(r) for r in rows]


def get_subtitle_markers() -> list[str]:
    raw = get_setting("subtitle_markers")
    if not raw:
        return list(config._DEFAULT_SUBTITLE_MARKERS)
    try:
        val = json.loads(raw)
        return [str(m) for m in val] if isinstance(val, list) else []
    except (ValueError, TypeError):
        return []


def load_language_config():
    """Lê idiomas + marcadores de legenda do banco e instala em config (runtime)."""
    langs = {}
    for row in list_languages():
        langs[row["code"]] = {
            "label": row["label"], "tmdb": row["tmdb"],
            "markers_strong": row["markers_strong"],
            "markers_weak": row["markers_weak"],
        }
    config.install_language_config(langs, get_subtitle_markers())


def save_languages(languages: list[dict], subtitle_markers: list[str]):
    """Substitui TODA a configuração de idiomas + marcadores e recarrega o runtime."""
    with _lock:
        _conn.execute("DELETE FROM languages")
        for i, lg in enumerate(languages):
            _conn.execute(
                "INSERT INTO languages (code, label, tmdb, markers_strong, "
                "markers_weak, sort_order) VALUES (?, ?, ?, ?, ?, ?)",
                (lg["code"], lg["label"], lg["tmdb"],
                 json.dumps(lg["markers_strong"], ensure_ascii=False),
                 json.dumps(lg["markers_weak"], ensure_ascii=False), i))
        _conn.commit()
    set_setting("subtitle_markers", json.dumps(subtitle_markers, ensure_ascii=False))
    load_language_config()


# -------------------- candidatos de busca (tabelas do job) --------------------
# Formato em memória e semântica: services/candidates.py. Uma linha por
# candidato (o rastro de cada consulta, a lista escolhível, as reservas e o
# ativo são colunas da MESMA linha) e a posição de cada um no ranking de cada
# episódio (série) em candidate_episodes.

def _create_candidate_tables():
    _conn.execute("""
        CREATE TABLE IF NOT EXISTS search_queries (
            job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
            n INTEGER NOT NULL,
            role TEXT NOT NULL,
            query TEXT NOT NULL,
            ts TEXT,
            results INTEGER,
            PRIMARY KEY (job_id, n)
        )""")
    _conn.execute("""
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
    _conn.execute("CREATE INDEX IF NOT EXISTS idx_search_candidates_query "
                  "ON search_candidates(job_id, query_n)")
    _conn.execute("""
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

# hash das últimas linhas GRAVADAS das seções de cada job: o upsert só
# reescreve as linhas quando elas mudaram (a maioria das gravações do job é
# progresso/estado — candidatos mudam na busca, na escolha e nas trocas)
_section_hash: dict[str, str] = {}


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
    _conn.execute("DELETE FROM search_candidates WHERE job_id = ?", (job_id,))
    _conn.execute("DELETE FROM search_queries WHERE job_id = ?", (job_id,))
    _conn.executemany(
        "INSERT INTO search_queries (job_id, n, role, query, ts, results) "
        "VALUES (?, ?, ?, ?, ?, ?)", [(job_id,) + q for q in qs])
    cols = ", ".join(c for c, _ in _CAND_COLS)
    ph = ", ".join("?" for _ in range(len(_CAND_COLS) + 1))
    for vals, eps in cs:
        cur = _conn.execute(
            f"INSERT INTO search_candidates (job_id, {cols}) VALUES ({ph})",
            (job_id,) + vals)
        if eps:
            _conn.executemany(
                "INSERT INTO candidate_episodes (candidate_id, ep_key, rank) "
                "VALUES (?, ?, ?)", [(cur.lastrowid, k, pos) for k, pos in eps])


def _load_sections(job_ids: list[str]) -> dict[str, dict]:
    """{job_id: {search_queries, search_candidates}} lidos das tabelas (com
    _lock já tomado). Em lotes, para a lista de jobs não virar N consultas."""
    out = {jid: {"search_queries": [], "search_candidates": []} for jid in job_ids}
    cols = ", ".join(c for c, _ in _CAND_COLS)
    for i in range(0, len(job_ids), 500):
        chunk = job_ids[i:i + 500]
        ph = ",".join("?" for _ in chunk)
        for jid, n, role, query, ts, results in _conn.execute(
                f"SELECT job_id, n, role, query, ts, results FROM search_queries "
                f"WHERE job_id IN ({ph}) ORDER BY job_id, n", chunk):
            out[jid]["search_queries"].append(
                {"n": n, "role": role, "query": query, "ts": ts, "results": results})
        by_rowid: dict[int, dict] = {}
        for r in _conn.execute(
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
        for cand_id, key, pos in _conn.execute(
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


def _compose(doc: dict, sections: dict | None) -> dict:
    """Documento de jobs.data + linhas das tabelas -> o dict do job."""
    if _is_legacy(doc):
        # gravado antes das tabelas: as linhas ainda não existem; o próximo
        # upsert (job ativo) grava no formato novo
        return _legacy_candidates(doc)
    doc.update(sections or {"search_queries": [], "search_candidates": []})
    # o que acabou de ser lido É o que está gravado: o próximo upsert sem
    # mudança nos candidatos não reescreve as linhas
    _section_hash[doc["id"]] = _rows_digest(_section_rows(doc))
    return doc


def _prepare_upsert(job: dict) -> tuple[str, tuple | None, str | None]:
    """(data, linhas das seções, hash) do job — tudo serializado ANTES de
    tocar no banco: dict mexido por outra thread no meio (RuntimeError) não
    pode deixar gravação pela metade."""
    doc = {k: v for k, v in job.items()
           if k != "events" and k not in _SECTIONS and k not in _LEGACY_KEYS}
    data = json.dumps(doc, ensure_ascii=False)
    # dict sem as seções (carregado sem elas, ou montado à mão) não mexe nas
    # linhas gravadas — só quem tem as listas pode reescrevê-las
    rows = _section_rows(job) if all(k in job for k in _SECTIONS) else None
    return data, rows, (_rows_digest(rows) if rows is not None else None)


def _apply_upsert(job: dict, prepared: tuple, create: bool) -> bool:
    """Escreve o job preparado (com _lock, dentro de uma transação aberta;
    quem chama faz o commit). False = linha inexistente com create=False."""
    data, rows, digest = prepared
    if create:
        _conn.execute(
            "INSERT INTO jobs (id, created_at, status, data) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET status = excluded.status, data = excluded.data",
            (job["id"], job["created_at"], job["status"], data))
    else:
        cur = _conn.execute("UPDATE jobs SET status = ?, data = ? WHERE id = ?",
                            (job["status"], data, job["id"]))
        if cur.rowcount == 0:
            return False
    if rows is not None and _section_hash.get(job["id"]) != digest:
        _write_sections(job["id"], rows)
    return True


def upsert_job(job: dict, create: bool = True):
    """Grava o job. Com create=False só ATUALIZA uma linha existente: é o que o
    flush atrasado de jobs sujos usa (jobs.runtime) — se o job foi removido no
    meio da janela, a gravação atrasada não pode recriá-lo (nem as linhas das
    tabelas filhas: sem a linha em `jobs`, nada é inserido).

    As seções em tabela própria (candidatos) só são reescritas quando mudaram
    desde a última gravação (hash por job)."""
    prepared = _prepare_upsert(job)
    with _lock:
        try:
            if not _apply_upsert(job, prepared, create):
                _conn.rollback()
                return
            _conn.commit()
        except BaseException:
            _conn.rollback()
            raise
        if prepared[2] is not None:
            _section_hash[job["id"]] = prepared[2]


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


def migrate_candidate_events(job_id: str) -> int:
    """Leva os eventos "candidates" antigos de um job para as tabelas (ver
    _legacy_candidate_events), numa transação só com a gravação do job.
    Devolve quantos eventos mudaram. Para a migração final do banco; até lá a
    lupa mostra o evento antigo como está (lista inline)."""
    job = get_job(job_id)
    if job is None:
        return 0
    with _lock:
        rows = _conn.execute(
            "SELECT id, ts, kind, data FROM events WHERE job_id = ? "
            "AND kind = 'candidates' ORDER BY id", (job_id,)).fetchall()
    events = [{"id": r[0], "ts": r[1], "kind": r[2],
               "data": json.loads(r[3]) if r[3] else None} for r in rows]
    new_doc, updates = _legacy_candidate_events(job, events)
    if not updates:
        return 0
    prepared = _prepare_upsert(new_doc)
    with _lock:
        try:
            if not _apply_upsert(new_doc, prepared, create=False):
                _conn.rollback()
                return 0
            _conn.executemany(
                "UPDATE events SET data = ? WHERE id = ?",
                [(json.dumps(d, ensure_ascii=False), eid) for eid, d in updates])
            _conn.commit()
        except BaseException:
            _conn.rollback()
            raise
        _section_hash[job_id] = prepared[2]
    return len(updates)


def add_event(job_id: str, ev: dict):
    with _lock:
        _conn.execute(
            "INSERT INTO events (job_id, ts, kind, message, data) VALUES (?, ?, ?, ?, ?)",
            (job_id, ev["ts"], ev["kind"], ev["message"],
             json.dumps(ev["data"], ensure_ascii=False) if "data" in ev else None))
        _conn.commit()


def _load_docs(rows: list, with_sections: bool = True) -> list[dict]:
    """Linhas (id, data) de `jobs` -> dicts dos jobs, com as seções em tabela
    (chamado com _lock). Sem as seções (listas que não as usam), o dict sai
    sem as chaves — nunca deve ser regravado."""
    docs = [json.loads(r[1]) for r in rows]
    if not with_sections:
        return [_legacy_candidates(d) if _is_legacy(d) else d for d in docs]
    sections = _load_sections([r[0] for r in rows])
    return [_compose(d, sections.get(r[0])) for d, r in zip(docs, rows)]


def load_jobs() -> list[dict]:
    with _lock:
        rows = _conn.execute("SELECT id, data FROM jobs").fetchall()
        return _load_docs(rows)


# mesma expressão do índice idx_jobs_media (jobs antigos, sem a chave, = filme)
_MEDIA_EXPR = "IFNULL(json_extract(data, '$.media_type'), 'movie')"


def load_jobs_by_status(statuses: tuple[str, ...], limit: int | None = None,
                        offset: int = 0, media: str | None = None,
                        sections: bool = True) -> list[dict]:
    """Carrega os jobs cujo status está na lista (ex.: só os ativos, ou só os
    terminais). Usa a coluna `status` indexada em vez de ler tudo.

    Com `limit`, pagina no SQL (ordenado por created_at desc, o mesmo critério
    da tela) — assim uma biblioteca com milhares de jobs terminais não vira um
    JSON gigante a cada abertura da lista. `media` ("movie"/"tv") filtra pela
    dimensão de mídia direto no SQL. `sections=False` não carrega os
    candidatos (telas que não os mostram — o resumo do cabeçalho).
    """
    if not statuses:
        return []
    ph = ",".join("?" for _ in statuses)
    sql = f"SELECT id, data FROM jobs WHERE status IN ({ph})"
    params: list = list(statuses)
    if media is not None:
        sql += f" AND {_MEDIA_EXPR} = ?"
        params.append(media)
    sql += " ORDER BY created_at DESC"
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        params += [limit, offset]
    with _lock:
        rows = _conn.execute(sql, params).fetchall()
        return _load_docs(rows, sections)


def get_job(job_id: str) -> dict | None:
    """Carrega um único job do banco (usado para jobs terminais que não ficam
    mais em memória)."""
    with _lock:
        row = _conn.execute(
            "SELECT id, data FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return _load_docs([row])[0] if row else None


def count_jobs_by_status(media: str | None = None) -> dict[str, int]:
    """{status: quantidade} — para os badges de contagem sem baixar as listas.

    `media` ("movie"/"tv") restringe a contagem à dimensão de mídia."""
    sql = "SELECT status, COUNT(*) FROM jobs"
    params: list = []
    if media is not None:
        sql += f" WHERE {_MEDIA_EXPR} = ?"
        params.append(media)
    sql += " GROUP BY status"
    with _lock:
        rows = _conn.execute(sql, params).fetchall()
    return {status: n for status, n in rows}


def error_jobs_for(tmdb_id: int, language: str, kind: str) -> list[str]:
    """IDs dos jobs em 'error' que casam tmdb_id + idioma + kind.

    Usado ao (re)adicionar um filme: um erro anterior daquela mesma variante é
    descartado (o novo job o substitui). Concluído/cancelado NÃO entram aqui —
    persistem mesmo com readição.
    """
    with _lock:
        rows = _conn.execute(
            "SELECT id, data FROM jobs WHERE status = 'error'").fetchall()
    out = []
    for jid, raw in rows:
        try:
            d = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if (d.get("tmdb_id") == tmdb_id and d.get("language") == language
                and d.get("kind", "both") == kind):
            out.append(jid)
    return out


def load_events(job_id: str) -> list[dict]:
    with _lock:
        rows = _conn.execute(
            "SELECT ts, kind, message, data FROM events WHERE job_id = ? "
            "ORDER BY id DESC LIMIT ?", (job_id, MAX_EVENTS_RETURNED)).fetchall()
    events = []
    for ts, kind, message, data in reversed(rows):
        ev = {"ts": ts, "kind": kind, "message": message}
        if data is not None:
            ev["data"] = json.loads(data)
        events.append(ev)
    return events


def delete_job(job_id: str):
    with _lock:
        _conn.execute("DELETE FROM events WHERE job_id = ?", (job_id,))
        # tabelas filhas (candidatos...) saem pelo ON DELETE CASCADE
        _conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
        _conn.commit()
        _section_hash.pop(job_id, None)


def _migrate_from_json():
    path = config.JOBS_FILE
    if not path.exists():
        return
    backup = path.parent / (path.name + ".bak")
    try:
        old_jobs = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        path.replace(backup)  # corrompido: preserva para inspecao e segue
        return
    with _lock:
        existing = {r[0] for r in _conn.execute("SELECT id FROM jobs").fetchall()}
    for job in old_jobs:
        if job.get("id") in existing:
            continue
        events = job.pop("events", None) or []
        upsert_job(job)
        for ev in events:
            add_event(job["id"], ev)
    path.replace(backup)
