"""Sessões de login (cookie da UI).

Só o HASH do token fica no banco (quem lê o jobs.db não ganha uma sessão
viva) e o último uso, para a expiração por inatividade. Persistir é o que
faz um deploy/reinício NÃO derrubar quem está logado.
"""
from services import store


def _create_session_tables():
    store._conn.execute("""
        CREATE TABLE IF NOT EXISTS auth_sessions (
            token_hash TEXT PRIMARY KEY,
            created_at REAL NOT NULL,
            last_seen REAL NOT NULL
        )""")


def load_sessions() -> dict[str, float]:
    """{hash do token: último uso (epoch)}."""
    if store._conn is None:
        return {}
    with store._lock:
        rows = store._conn.execute(
            "SELECT token_hash, last_seen FROM auth_sessions").fetchall()
    return {h: float(t) for h, t in rows}


def save_session(token_hash: str, now: float):
    if store._conn is None:
        return
    with store._lock:
        store._conn.execute(
            "INSERT INTO auth_sessions (token_hash, created_at, last_seen) "
            "VALUES (?, ?, ?) ON CONFLICT(token_hash) DO UPDATE SET "
            "last_seen = excluded.last_seen", (token_hash, now, now))
        store._conn.commit()


def touch_session(token_hash: str, now: float):
    if store._conn is None:
        return
    with store._lock:
        store._conn.execute("UPDATE auth_sessions SET last_seen = ? WHERE token_hash = ?",
                            (now, token_hash))
        store._conn.commit()


def delete_sessions(token_hashes: list[str] | None = None, older_than: float | None = None):
    """Apaga as sessões dadas, as sem uso desde `older_than`, ou TODAS
    (nenhum dos dois)."""
    if store._conn is None:
        return
    with store._lock:
        if token_hashes is not None:
            store._conn.executemany("DELETE FROM auth_sessions WHERE token_hash = ?",
                                    [(h,) for h in token_hashes])
        elif older_than is not None:
            store._conn.execute("DELETE FROM auth_sessions WHERE last_seen < ?",
                                (older_than,))
        else:
            store._conn.execute("DELETE FROM auth_sessions")
        store._conn.commit()
