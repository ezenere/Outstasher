"""Envio de torrents ao qBittorrent não derruba o job.

Caso real: num job de série com 42 torrents, o link de um deles (resolvido
no Jackett, que vai ao tracker) passou de 60 s — ReadTimeout — e o job
INTEIRO morreu no meio do envio. Agora cada envio tem tentativas e, se ainda
assim não for, o torrent fica para o watchdog reinserir.
"""
import asyncio

import httpx
import pytest

from services import jobs
from services.jobs import runtime
from services.series import pipeline


class FakeQbit:
    """add() que falha N vezes para as URLs marcadas."""

    def __init__(self, falhas: dict[str, int] | None = None):
        self.falhas = dict(falhas or {})
        self.adds: list[tuple[str, str]] = []

    async def add(self, url, tags, save_path=None):
        if self.falhas.get(url, 0) > 0:
            self.falhas[url] -= 1
            raise httpx.ReadTimeout("")
        self.adds.append((url, tags))


@pytest.fixture(autouse=True)
def sem_espera(monkeypatch):
    monkeypatch.setattr(runtime, "SEND_RETRY_DELAYS_S", (0.0, 0.0))


def test_tenta_de_novo_antes_de_desistir():
    q = FakeQbit({"magnet:a": 2})
    assert asyncio.run(runtime.send_torrent(q, "magnet:a", "t", None)) is None
    assert q.adds == [("magnet:a", "t")]


def test_desiste_com_o_motivo():
    q = FakeQbit({"magnet:a": 99})
    erro = asyncio.run(runtime.send_torrent(q, "magnet:a", "t", None))
    assert erro == "ReadTimeout"
    assert q.adds == []


def _job_serie():
    def t(n, url, eps):
        return {"n": n, "tag": f"t{n}", "role": "original", "state": "pending",
                "title": f"Serie Exemplo pack {n}", "magnet": url,
                "coverage": eps, "progress": None}
    return {"id": "envio1", "media_type": "tv", "status": "searching", "detail": "",
            "created_at": "2026-01-01T00:00:00", "progress": {},
            "episodes": {"S01E01": {"state": "pending"}, "S01E02": {"state": "pending"}},
            "torrents": [t(0, "magnet:lento", ["S01E01"]), t(1, "magnet:ok", ["S01E02"])]}


def test_serie_link_lento_nao_derruba_o_job(temp_db, monkeypatch):
    q = FakeQbit({"magnet:lento": 99})
    monkeypatch.setattr(jobs, "_qbit", q)
    job = _job_serie()
    jobs._jobs[job["id"]] = job
    monkeypatch.setattr(pipeline, "_active_refs", lambda j: [(1, 1), (1, 2)])
    acompanhou = []

    async def watchdog(j):
        acompanhou.append([t["state"] for t in j["torrents"]])

    monkeypatch.setattr(pipeline, "_wait_downloads", watchdog)
    msgs = []
    real_event = jobs._event
    monkeypatch.setattr(jobs, "_event", lambda j, k, m, *a: (msgs.append(m), real_event(j, k, m, *a)))
    asyncio.run(pipeline._start_downloads(job))
    # o segundo foi enviado mesmo com o primeiro falhando, e os dois entram
    # no acompanhamento (que reinsere o que não está no qBittorrent)
    assert q.adds == [("magnet:ok", f"{pipeline.shared_tag(job)},t1")]
    assert acompanhou == [["downloading", "downloading"]]
    assert any("t0 não foi enviado (ReadTimeout)" in m for m in msgs), msgs
    assert any(m.startswith("Torrent enviado") and "pack 1" in m for m in msgs)
    assert job["status"] != "error"
