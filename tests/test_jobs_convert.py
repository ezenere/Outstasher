"""Plumbing das opções de conversão nos jobs: create/retry/_slim_job/persistência.

Usa as capacidades REAIS do ffmpeg (hevc/opus existem; vvc só se houver
libvvenc). Async sem pytest-asyncio: cada teste roda a coroutine com asyncio.run.
"""
import asyncio

import pytest

from services import jobs, transcode

CONV = {"video_codec": "hevc", "video_bitrate": 4000, "audio_codec": "opus",
        "audio_bitrate": 128, "subtitles": "none"}


@pytest.fixture
def no_pipeline(temp_db, monkeypatch):
    """Neutraliza o pipeline real (TMDB/Jackett/qBittorrent) — os testes só
    exercitam a criação/normalização/persistência do job."""
    async def _noop(job):
        return None
    monkeypatch.setattr(jobs, "_run", _noop)
    return temp_db


def test_create_with_convert_normalizes_and_persists(no_pipeline):
    async def go():
        job = await jobs.create(1, "pt", convert=CONV)
        full = jobs._jobs[job["id"]]
        assert full["convert"]["video_codec"] == "hevc"
        assert full["convert"]["preset"] == "default"  # defaults preenchidos
        assert full["convert"]["subtitles"] == "none"
        assert jobs._slim_job(full)["convert"] is True
    asyncio.run(go())


def test_create_without_convert(no_pipeline):
    async def go():
        job = await jobs.create(2, "pt")
        full = jobs._jobs[job["id"]]
        assert full["convert"] is None
        assert jobs._slim_job(full)["convert"] is False
    asyncio.run(go())


def test_download_only_discards_convert(no_pipeline):
    async def go():
        job = await jobs.create(3, "pt", download_only=True, convert=CONV)
        assert jobs._jobs[job["id"]]["convert"] is None
    asyncio.run(go())


def test_create_rejects_invalid_convert(no_pipeline, real_encoders):
    bads = [{"video_codec": "h264"},   # falta bitrate
            {"audio_codec": "mp3"}]    # não oferecido
    if "libvvenc" not in real_encoders:
        bads.append({"video_codec": "vvc", "video_bitrate": 4000})  # sem encoder

    async def go():
        for bad in bads:
            with pytest.raises(ValueError):
                await jobs.create(4, "pt", convert=bad)
    asyncio.run(go())


def test_retry_preserves_convert(no_pipeline, monkeypatch):
    async def go():
        job = await jobs.create(1, "pt", convert=CONV)
        # simula o job em erro, relido do banco
        old = dict(jobs._jobs[job["id"]])
        old["status"] = "error"
        no_pipeline.upsert_job(old)
        jobs._jobs.pop(old["id"], None)

        captured = {}

        async def spy_create(*args, **kwargs):
            captured["args"] = args
            return {"id": "fake"}

        monkeypatch.setattr(jobs, "create", spy_create)
        await jobs.retry(old["id"])
        # o convert (último posicional) é repassado ao create
        assert captured["args"][-1]["video_codec"] == "hevc", captured
    asyncio.run(go())


def test_slim_progress_carries_size(no_pipeline):
    """Os cards da lista mostram % + baixado/total: o _slim_job precisa levar
    downloaded/size/state junto do percentual (velocidade/ETA/seeds não)."""
    async def go():
        job = await jobs.create(1, "pt")
        full = jobs._jobs[job["id"]]
        full["progress"]["video"] = {
            "pct": 42.5, "speed": 5e6, "eta": 300, "state": "downloading",
            "seeds": 12, "name": "f", "size": 8_000_000_000, "downloaded": 3_400_000_000}
        p = jobs._slim_job(full)["progress"]["video"]
        assert p == {"pct": 42.5, "downloaded": 3_400_000_000,
                     "size": 8_000_000_000, "state": "downloading"}

        # job antigo do banco: progresso pode ser só o número (sem tamanhos)
        full["progress"]["audio"] = 77.0
        assert jobs._slim_job(full)["progress"]["audio"] == {
            "pct": 77.0, "downloaded": None, "size": None, "state": None}

        # sem progresso ainda (searching): nada de barra
        full["progress"]["video"] = None
        assert jobs._slim_job(full)["progress"]["video"] is None
    asyncio.run(go())


def test_convert_persisted_in_db(no_pipeline):
    async def go():
        job = await jobs.create(1, "pt", convert=CONV)
        from_db = no_pipeline.get_job(job["id"])
        assert (from_db.get("convert") or {}).get("video_codec") == "hevc"
        job2 = await jobs.create(2, "pt")
        assert "convert" in no_pipeline.get_job(job2["id"])
    asyncio.run(go())
