"""Cancelar durante a conversão (ffmpeg REAL): mata o ffmpeg e apaga o parcial.

Marcado com @pytest.mark.ffmpeg. Roda uma conversão lenta de verdade, cancela no
meio e verifica que o subprocess morre, o arquivo final some e o status vira
'cancelled' (não 'error').
"""
import asyncio
import subprocess
from pathlib import Path

import pytest

from services import jobs, merger

pytestmark = pytest.mark.ffmpeg


def _make_long(path, lang, dur=60, w=1280, h=536):
    """MKV longo (60s) para a conversão demorar o bastante para cancelar no meio."""
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", f"testsrc=s={w}x{h}:d={dur}:r=24",
         "-f", "lavfi", "-i", f"sine=frequency=440:duration={dur}",
         "-c:v", "libx264", "-preset", "ultrafast", "-b:v", "2M",
         "-pix_fmt", "yuv420p", "-c:a", "ac3", "-b:a", "192k",
         "-metadata:s:a:0", f"language={lang}", str(path)],
        check=True)


def test_cancel_kills_ffmpeg_and_deletes_output(temp_db, tmp_path, monkeypatch):
    # merge com conversão pesada (CRF veryslow) força um re-encode lento; o
    # offset é fixado para o foco ser o cancelamento, não o alinhamento
    monkeypatch.setattr(merger, "_measure_offset", lambda *a, **k: 0.0)

    f1, f2 = tmp_path / "orig.mkv", tmp_path / "dub.mkv"
    _make_long(f1, "eng")
    _make_long(f2, "por")
    dest = tmp_path / "dest"
    dest.mkdir()

    job = {
        "id": "cxl1", "tmdb_id": 1, "language": "pt", "mode": "auto", "kind": "both",
        "convert": {"video_codec": "h264", "quality_mode": "crf", "crf": 20,
                    "preset": "veryslow"},
        "status": "merging", "detail": "",
        "movie": {"original_title": "Teste", "year": "2020", "original_language": "en"},
        "video_torrent": None, "audio_torrent": None, "output": None,
        "progress": {"video": None, "audio": None, "merge": None},
        "destination_id": 1, "destination_label": "d", "destination_path": str(dest),
        "created_at": "2026-01-01T00:00:00", "search": None, "fallbacks": None, "current": None,
    }
    jobs._jobs["cxl1"] = job
    temp_db.upsert_job(job)

    async def run_merge():
        try:
            async with jobs._get_merge_lock():
                await jobs._merge(job, f1, f2)
        except Exception as e:  # noqa: BLE001
            jobs._fail(job, f"{type(e).__name__}: {e}")

    async def go():
        task = jobs._spawn("cxl1", run_merge())

        # espera o ffmpeg subir e começar a escrever o arquivo parcial
        for _ in range(300):
            await asyncio.sleep(0.1)
            proc = jobs._ffmpeg_procs.get("cxl1")
            out = Path(job["output"]) if job.get("output") else None
            if proc and out and out.exists() and out.stat().st_size > 0:
                break
        else:
            pytest.fail("ffmpeg não começou a escrever a tempo")

        proc = jobs._ffmpeg_procs["cxl1"]
        out_path = Path(job["output"])
        assert proc.poll() is None  # ffmpeg vivo antes de cancelar

        assert await jobs.cancel("cxl1", delete_torrents=False) is not None

        # 1. ffmpeg morto (kill é assíncrono; dá um tempinho para o poll refletir)
        for _ in range(30):
            if proc.poll() is not None:
                break
            await asyncio.sleep(0.1)
        assert proc.poll() is not None, "ffmpeg deveria ter sido morto"
        assert "cxl1" not in jobs._ffmpeg_procs

        # 2. arquivo final apagado + subpasta vazia removida
        assert not out_path.exists(), out_path
        assert not out_path.parent.exists()

        # 3. status cancelled (não error)
        assert temp_db.get_job("cxl1")["status"] == "cancelled"
        assert "cxl1" not in jobs._cancelling

        # 4. task encerrada
        for _ in range(50):
            if task.done():
                break
            await asyncio.sleep(0.1)
        assert task.done()

    asyncio.run(go())
