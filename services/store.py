"""Persistencia em SQLite (jobs.db).

- jobs: um documento JSON por job (upsert pequeno a cada mudanca).
- events: append-only, um INSERT por evento — nada de regravar tudo.
- WAL + lock: seguro para o event loop e para a thread do merge.
- Migracao automatica: se existir jobs.json (formato antigo), importa e
  renomeia para jobs.json.bak.
"""
import json
import sqlite3
import threading

import config

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
    _conn.execute("""
        CREATE TABLE IF NOT EXISTS destinations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT NOT NULL,
            path TEXT NOT NULL,
            is_default INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )""")
    _conn.execute("""
        CREATE TABLE IF NOT EXISTS torrent_targets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT NOT NULL,
            save_path TEXT NOT NULL DEFAULT '',
            local_path TEXT NOT NULL DEFAULT '',
            is_default INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )""")
    _conn.commit()
    _migrate_from_json()
    _seed_default_destination()
    _seed_default_torrent_target()


# -------------------- destinos (pastas de destino do arquivo final) --------------------

def list_destinations() -> list[dict]:
    with _lock:
        rows = _conn.execute(
            "SELECT id, label, path, is_default FROM destinations "
            "ORDER BY is_default DESC, label COLLATE NOCASE").fetchall()
    return [{"id": r[0], "label": r[1], "path": r[2], "is_default": bool(r[3])} for r in rows]


def get_destination(dest_id: int) -> dict | None:
    with _lock:
        r = _conn.execute(
            "SELECT id, label, path, is_default FROM destinations WHERE id = ?",
            (dest_id,)).fetchone()
    return {"id": r[0], "label": r[1], "path": r[2], "is_default": bool(r[3])} if r else None


def default_destination() -> dict | None:
    with _lock:
        r = _conn.execute(
            "SELECT id, label, path, is_default FROM destinations "
            "ORDER BY is_default DESC, id LIMIT 1").fetchone()
    return {"id": r[0], "label": r[1], "path": r[2], "is_default": bool(r[3])} if r else None


def add_destination(label: str, path: str, is_default: bool = False) -> dict:
    from datetime import datetime
    with _lock:
        if is_default:
            _conn.execute("UPDATE destinations SET is_default = 0")
        cur = _conn.execute(
            "INSERT INTO destinations (label, path, is_default, created_at) "
            "VALUES (?, ?, ?, ?)",
            (label, path, 1 if is_default else 0,
             datetime.now().isoformat(timespec="seconds")))
        _conn.commit()
        dest_id = cur.lastrowid
    return get_destination(dest_id)


def update_destination(dest_id: int, label: str, path: str, is_default: bool) -> dict | None:
    with _lock:
        exists = _conn.execute(
            "SELECT 1 FROM destinations WHERE id = ?", (dest_id,)).fetchone()
        if not exists:
            return None
        if is_default:
            _conn.execute("UPDATE destinations SET is_default = 0")
        _conn.execute(
            "UPDATE destinations SET label = ?, path = ?, is_default = ? WHERE id = ?",
            (label, path, 1 if is_default else 0, dest_id))
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


def upsert_job(job: dict):
    doc = {k: v for k, v in job.items() if k != "events"}
    with _lock:
        _conn.execute(
            "INSERT INTO jobs (id, created_at, status, data) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET status = excluded.status, data = excluded.data",
            (job["id"], job["created_at"], job["status"],
             json.dumps(doc, ensure_ascii=False)))
        _conn.commit()


def add_event(job_id: str, ev: dict):
    with _lock:
        _conn.execute(
            "INSERT INTO events (job_id, ts, kind, message, data) VALUES (?, ?, ?, ?, ?)",
            (job_id, ev["ts"], ev["kind"], ev["message"],
             json.dumps(ev["data"], ensure_ascii=False) if "data" in ev else None))
        _conn.commit()


def load_jobs() -> list[dict]:
    with _lock:
        rows = _conn.execute("SELECT data FROM jobs").fetchall()
    return [json.loads(r[0]) for r in rows]


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
        _conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
        _conn.commit()


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
