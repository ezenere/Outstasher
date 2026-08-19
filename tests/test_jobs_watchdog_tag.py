"""Watchdog com mais de um torrent na mesma tag (troca que deixou sobra):
vale o mais RECENTE, não o primeiro que o qBittorrent devolver."""
import asyncio

from services import jobs
from services.jobs import downloads


def test_watchdog_pega_o_torrent_mais_recente_da_tag(temp_db, monkeypatch):
    job = {"id": "wd1", "kind": "dubbed", "status": "downloading", "detail": "",
           "progress": {"video": None, "audio": None}, "movie": None,
           "created_at": "2026-01-01T00:00:00", "tmdb_id": 1, "language": "pt",
           "mode": "auto", "output": None, "search": None, "fallbacks": {},
           "current": {}, "video_torrent": None, "audio_torrent": None}
    jobs._jobs["wd1"] = job

    def torrent(h, name, added_on, path):
        return {"hash": h, "progress": 1.0, "state": "stalledUP", "name": name,
                "content_path": path, "added_on": added_on, "size": 1,
                "completed": 1, "dlspeed": 0, "eta": 0, "num_seeds": 0}

    class Qbit:
        async def info_by_tag(self, tag):
            # o ANTIGO vem primeiro na resposta, como no caso real
            return [torrent("velho", "torrent antigo", 100, "/x/antigo"),
                    torrent("novo", "torrent novo", 200, "/x/novo")]

    monkeypatch.setattr(jobs.runtime, "_qbit", Qbit())

    async def go():
        return await asyncio.wait_for(downloads._wait_downloads(job), timeout=10)

    paths = asyncio.run(go())
    assert paths == {"audio": "/x/novo"}, paths
    assert job["progress"]["audio"]["name"] == "torrent novo"
