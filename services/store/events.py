"""Eventos do job: append-only, um INSERT por evento (add_event), e a leitura
dos N mais recentes para o detalhe (load_events).
"""
import json

from services import store


MAX_EVENTS_RETURNED = 500  # o detalhe do job devolve os N eventos mais recentes


def add_event(job_id: str, ev: dict):
    with store._lock:
        store._conn.execute(
            "INSERT INTO events (job_id, ts, kind, message, data) VALUES (?, ?, ?, ?, ?)",
            (job_id, ev["ts"], ev["kind"], ev["message"],
             json.dumps(ev["data"], ensure_ascii=False) if "data" in ev else None))
        store._conn.commit()


def load_events(job_id: str) -> list[dict]:
    with store._lock:
        rows = store._conn.execute(
            "SELECT ts, kind, message, data FROM events WHERE job_id = ? "
            "ORDER BY id DESC LIMIT ?", (job_id, MAX_EVENTS_RETURNED)).fetchall()
    events = []
    for ts, kind, message, data in reversed(rows):
        ev = {"ts": ts, "kind": kind, "message": message}
        if data is not None:
            ev["data"] = json.loads(data)
        events.append(ev)
    return events
