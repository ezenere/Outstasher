"""Opção 'apenas baixar': o download conclui o job, sem conversão/hardlink/cópia."""
import asyncio

import pytest

from services import jobs


def mkjob(**kw):
    j = {"id": "do1", "tmdb_id": 1, "language": "pt", "mode": "auto", "kind": "both",
         "download_only": True, "convert": None, "status": "downloading", "detail": "",
         "movie": None, "created_at": "2026-01-01T00:00:00",
         "video_torrent": None, "audio_torrent": None, "output": None,
         "progress": {"video": {"pct": 100.0}, "audio": {"pct": 100.0}},
         "search": None, "fallbacks": None, "current": None}
    j.update(kw)
    return j


def test_run_from_download_finishes_without_conversion(temp_db, monkeypatch):
    async def fake_wait(job):
        return {"video": "/dl/Filme.1080p/f.mkv", "audio": "/dl/Filme.Dublado/f.mkv"}

    async def boom_resolve(job, content, kind):
        raise AssertionError("_resolve_video_file não devia rodar com apenas baixar")

    async def boom_merge(job, v, a, allow_drift=False):
        raise AssertionError("_merge não devia rodar com apenas baixar")

    class Boom:  # qualquer chamada ao qBittorrent após o download = erro
        def __getattr__(self, name):
            raise AssertionError(f"qBittorrent chamado ({name}) após download-only!")

    monkeypatch.setattr(jobs, "_wait_downloads", fake_wait)
    monkeypatch.setattr(jobs, "_resolve_video_file", boom_resolve)
    monkeypatch.setattr(jobs, "_merge", boom_merge)
    monkeypatch.setattr(jobs, "_qbit", Boom())

    job = mkjob()
    jobs._jobs["do1"] = job
    temp_db.upsert_job(job)
    asyncio.run(jobs._run_from_download(job))

    db = temp_db.get_job("do1")
    assert db["status"] == "done"
    assert db["output"] == "/dl/Filme.1080p/f.mkv | /dl/Filme.Dublado/f.mkv"
    assert "do1" not in jobs._jobs  # terminal: sai da memória


def test_single_kind_output_is_just_the_path(temp_db, monkeypatch):
    async def fake_wait_a(job):
        return {"audio": "/dl/Filme.Dublado/f.mkv"}

    monkeypatch.setattr(jobs, "_wait_downloads", fake_wait_a)
    job = mkjob(id="do2", kind="dubbed", progress={"video": None, "audio": {"pct": 100.0}})
    jobs._jobs["do2"] = job
    asyncio.run(jobs._run_from_download(job))
    assert temp_db.get_job("do2")["output"] == "/dl/Filme.Dublado/f.mkv"


def test_create_download_only_without_destination(temp_db, monkeypatch):
    # cenário "nenhum destino cadastrado" (o init semeia um; removemos)
    for d in temp_db.list_destinations():
        temp_db.delete_destination(d["id"])
    assert temp_db.default_destination() is None

    async def fake_run(job):
        pass
    monkeypatch.setattr(jobs, "_run", fake_run)

    async def go():
        out = await jobs.create(2, "pt", "auto", None, None, "both", download_only=True)
        assert out["download_only"] is True
        assert out["destination_id"] is None and out["destination_path"] is None
        ev = temp_db.load_events(out["id"])
        assert "apenas baixar" in ev[0]["message"] and "destino:" not in ev[0]["message"]
        await jobs._tasks[out["id"]]
        # sem download_only e sem destino -> ValueError
        with pytest.raises(ValueError, match="Nenhum destino"):
            await jobs.create(2, "pt")
    asyncio.run(go())


def test_retry_preserves_download_only(temp_db, monkeypatch):
    err = mkjob(id="do3", status="error", download_only=True, kind="original",
                progress={"video": None, "audio": None})
    temp_db.upsert_job(err)

    captured = {}

    async def fake_create(tmdb_id, language, mode="auto", destination_id=None,
                          torrent_target_id=None, kind="both", download_only=False,
                          convert=None):
        captured.update(kind=kind, download_only=download_only)
        return {"id": "novo"}
    monkeypatch.setattr(jobs, "create", fake_create)

    async def go():
        out = await jobs.retry("do3")
        assert out == {"id": "novo"}
        assert captured == {"kind": "original", "download_only": True}
    asyncio.run(go())
