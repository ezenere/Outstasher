"""Relatório do processamento por episódio (e do filme): o que a ficha do
episódio mostra além do que o job já guarda.

Uma linha por (job, episódio) — o filme usa a chave REPORT_MOVIE_KEY — com um
JSON de forma livre: método usado (hardlink, stream copy, drift, EDL), as
janelas de offset medidas, o plano do render da EDL (fatias e cortes, cada um
com o motivo) e os arquivos de origem. Fica FORA do documento do job: só a
ficha lê, sob demanda, e o polling do job não carrega isso.

Também mora aqui a leitura do log de um episódio: eventos marcados com
data.ep (o runtime marca tudo o que acontece com o episódio em curso) e, nos
jobs de antes da marca, o recorte entre o fim do episódio anterior e o fim
deste — o merge de série roda UM episódio por vez, então o trecho é dele.
"""
import json
from datetime import datetime

from services import store

REPORT_MOVIE_KEY = ""
MAX_EPISODE_EVENTS = 600

# mensagens que FECHAM um episódio no log (concluído, falhou, conflito,
# revisão pendente): o recorte dos jobs antigos vai de uma à outra
_END_PATTERNS = ("✅ {k} %", "❌ {k} %", "⚠️ {k}: conflito%", "🔍 {k}: %")
_ANY_END = ("✅ S%", "❌ S%", "⚠️ S%: conflito%", "🔍 S%")


def _create_report_tables():
    store._conn.execute("""
        CREATE TABLE IF NOT EXISTS merge_reports (
            job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
            ep_key TEXT NOT NULL,
            data TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (job_id, ep_key)
        )""")


def load_report(job_id: str, ep_key: str) -> dict | None:
    with store._lock:
        row = store._conn.execute(
            "SELECT data FROM merge_reports WHERE job_id = ? AND ep_key = ?",
            (job_id, ep_key)).fetchone()
    return json.loads(row[0]) if row else None


def update_report(job_id: str, ep_key: str, fields: dict, reset: bool = False):
    """Junta `fields` no relatório (reset=True começa do zero: o episódio
    voltou ao início do pipeline). Chave com valor None sai do relatório."""
    with store._lock:
        row = None if reset else store._conn.execute(
            "SELECT data FROM merge_reports WHERE job_id = ? AND ep_key = ?",
            (job_id, ep_key)).fetchone()
        data = json.loads(row[0]) if row else {}
        for k, v in fields.items():
            if v is None:
                data.pop(k, None)
            else:
                data[k] = v
        store._conn.execute(
            "INSERT INTO merge_reports (job_id, ep_key, data, updated_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT (job_id, ep_key) DO UPDATE SET "
            "data = excluded.data, updated_at = excluded.updated_at",
            (job_id, ep_key, json.dumps(data, ensure_ascii=False),
             datetime.now().isoformat(timespec="seconds")))
        store._conn.commit()


def load_episode_events(job_id: str, ep_key: str) -> list[dict]:
    """Log de UM episódio: os eventos marcados com ele e, se o job é de antes
    da marca, o recorte da última passagem dele pelo merge."""
    with store._lock:
        rows = store._conn.execute(
            "SELECT id, ts, kind, message, data FROM events WHERE job_id = ? "
            "AND data IS NOT NULL AND json_valid(data) "
            "AND json_extract(data, '$.ep') = ? ORDER BY id DESC LIMIT ?",
            (job_id, ep_key, MAX_EPISODE_EVENTS)).fetchall()
        if not rows:
            rows = _legacy_slice(job_id, ep_key)
    out = []
    for _id, ts, kind, message, data in sorted(rows):
        ev = {"ts": ts, "kind": kind, "message": message}
        if data is not None:
            try:
                ev["data"] = json.loads(data)
            except ValueError:
                pass
        out.append(ev)
    return out


def _legacy_slice(job_id: str, ep_key: str) -> list[tuple]:
    """Recorte por posição no log (chamado com o lock): do evento que fechou
    o episódio ANTERIOR até o que fechou este. Sem fechamento (episódio em
    curso ou nunca processado), do último fechamento até agora — só se o
    episódio aparece nesse trecho."""
    conn = store._conn
    like_end = " OR ".join("message LIKE ?" for _ in _END_PATTERNS)
    end = conn.execute(
        f"SELECT MAX(id) FROM events WHERE job_id = ? AND kind = 'merge' "
        f"AND ({like_end})",
        (job_id, *(p.format(k=ep_key) for p in _END_PATTERNS))).fetchone()[0]
    like_any = " OR ".join("message LIKE ?" for _ in _ANY_END)
    if end is None:
        start = conn.execute(
            f"SELECT MAX(id) FROM events WHERE job_id = ? AND kind = 'merge' "
            f"AND ({like_any})", (job_id, *_ANY_END)).fetchone()[0] or 0
        hit = conn.execute(
            "SELECT 1 FROM events WHERE job_id = ? AND id > ? AND message LIKE ? "
            "LIMIT 1", (job_id, start, f"%{ep_key}%")).fetchone()
        if not hit:
            return []
        end = 1 << 62
    else:
        start = conn.execute(
            f"SELECT MAX(id) FROM events WHERE job_id = ? AND kind = 'merge' "
            f"AND id < ? AND ({like_any})", (job_id, end, *_ANY_END)).fetchone()[0] or 0
    return conn.execute(
        "SELECT id, ts, kind, message, data FROM events WHERE job_id = ? "
        "AND kind = 'merge' AND id > ? AND id <= ? ORDER BY id DESC LIMIT ?",
        (job_id, start, end, MAX_EPISODE_EVENTS)).fetchall()
