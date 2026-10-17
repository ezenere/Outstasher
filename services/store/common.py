"""Utilitários das seções em tabela: JSON opcional (_json), hash de partes já
prontas para a detecção de mudança (_digest) e consulta em lotes de ids
(_in_chunks).
"""
import hashlib
import json


def _json(v) -> str | None:
    return None if v is None else json.dumps(v, ensure_ascii=False)


def _digest(*parts) -> str:
    """Hash de partes já prontas (str entra crua, sem serializar de novo)."""
    h = hashlib.sha1()
    for p in parts:
        h.update(p.encode("utf-8") if isinstance(p, str)
                 else json.dumps(p, ensure_ascii=False).encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()


def _in_chunks(job_ids: list[str]):
    for i in range(0, len(job_ids), 500):
        chunk = job_ids[i:i + 500]
        yield chunk, ",".join("?" for _ in chunk)
