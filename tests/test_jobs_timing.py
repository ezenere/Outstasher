"""Tempo de conversão/cópia: _deliver_single grava merge_started_at e o expõe."""
import asyncio

from services import jobs


def test_deliver_single_records_merge_started_at(temp_db, tmp_path, monkeypatch):
    # entrega por hardlink (sem ffmpeg): mais rápido, exercita o mesmo carimbo
    src = tmp_path / "Filme.mkv"
    src.write_bytes(b"conteudo de video")
    dest = tmp_path / "dest"
    dest.mkdir()

    async def no_cleanup(job):
        pass
    monkeypatch.setattr(jobs, "_cleanup_torrents", no_cleanup)

    job = {
        "id": "t1", "tmdb_id": 1, "language": "pt", "mode": "auto", "kind": "original",
        "convert": None, "status": "downloading", "detail": "",
        "movie": {"original_title": "Filme", "year": "2020", "original_language": "en"},
        "video_torrent": None, "audio_torrent": None, "output": None,
        "progress": {"video": None, "audio": None},
        "destination_id": 1, "destination_label": "d", "destination_path": str(dest),
        "created_at": "2026-01-01T00:00:00", "search": None, "fallbacks": None, "current": None,
    }
    jobs._jobs["t1"] = job
    temp_db.upsert_job(job)

    asyncio.run(jobs._deliver_single(job, {"video": src}))

    # o carimbo foi gravado e sobrevive no banco / no tick de progresso
    assert job.get("merge_started_at")
    assert temp_db.get_job("t1")["merge_started_at"] == job["merge_started_at"]
    assert jobs.progress("t1")["merge_started_at"] == job["merge_started_at"]
    assert temp_db.get_job("t1")["status"] == "done"
