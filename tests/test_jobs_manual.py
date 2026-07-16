"""Conversão manual (create_manual): validação, pipeline, drift, retry, resume.

Plumbing puro — o merge/ffprobe/tmdb são substituídos por fakes; o que se testa
é a orquestração dos jobs, não o ffmpeg (isso vive em test_merge_ffmpeg.py).
"""
import asyncio

import pytest

from services import jobs, merger


@pytest.fixture
def files(tmp_path):
    vf, af = tmp_path / "video.mkv", tmp_path / "audio.mkv"
    vf.write_bytes(b"x")
    af.write_bytes(b"x")
    return vf, af


@pytest.fixture
def manual_env(temp_db, monkeypatch, files):
    """Destino cadastrado + fakes de probe/tmdb. Devolve (vf, af, state) onde
    state coleta os efeitos (probed/merged) para as asserções."""
    temp_db.add_destination("Filmes", str(files[0].parent / "out"), True)
    state = {"probed": [], "merged": []}

    def fake_probe(path, role):
        state["probed"].append((path.name, role))
    monkeypatch.setattr(jobs, "_probe_manual_file", fake_probe)

    async def fake_details(tmdb_id, lang):
        return {"original_title": "Ex Machina", "localized_title": "Ex Machina",
                "year": "2014", "poster": None}
    monkeypatch.setattr(jobs.tmdb, "details", fake_details)

    async def fake_merge(job, video_file, audio_file, allow_drift=False):
        state["merged"].append((str(video_file), str(audio_file), allow_drift))
        job["output"] = "/out/x.mkv"
        jobs._set(job, "done", "ok")
    monkeypatch.setattr(jobs, "_merge", fake_merge)

    return files[0], files[1], state


def test_validation_rejects_missing_and_same_file(manual_env):
    vf, af, _ = manual_env

    async def go():
        with pytest.raises(ValueError, match="não existe"):
            await jobs.create_manual(1, "pt", str(vf.parent / "nao_existe.mkv"), str(af))
        with pytest.raises(ValueError, match="mesmo arquivo"):
            await jobs.create_manual(1, "pt", str(vf), str(vf))
    asyncio.run(go())


@pytest.mark.parametrize("probe_result, role, msg_part", [
    ("__boom__", "video", "não parece"),                                    # não é mídia
    ({"streams": [{"codec_type": "audio"}]}, "video", "não tem stream de vídeo"),
    ({"streams": [{"codec_type": "video", "disposition": {"attached_pic": 1}},
                  {"codec_type": "audio"}]}, "video", "não tem stream de vídeo"),  # só capa
    ({"streams": [{"codec_type": "video"}]}, "video", "não tem stream de áudio"),
    ({"streams": [{"codec_type": "video"}]}, "audio", "não tem stream de áudio"),
])
def test_probe_manual_file_rejects(temp_db, monkeypatch, tmp_path, probe_result, role, msg_part):
    vf = tmp_path / "v.mkv"
    vf.write_bytes(b"x")
    if probe_result == "__boom__":
        def fake(p):
            raise merger.MergeError("nope")
    else:
        def fake(p):
            return probe_result
    monkeypatch.setattr(merger, "ffprobe_json", fake)
    with pytest.raises(ValueError, match=msg_part):
        jobs._probe_manual_file(vf, role)


def test_probe_manual_file_accepts_valid(temp_db, monkeypatch, tmp_path):
    vf, af = tmp_path / "v.mkv", tmp_path / "a.mka"
    vf.write_bytes(b"x")
    af.write_bytes(b"x")
    # vídeo+áudio no papel de vídeo
    monkeypatch.setattr(merger, "ffprobe_json",
                        lambda p: {"streams": [{"codec_type": "video"}, {"codec_type": "audio"}]})
    jobs._probe_manual_file(vf, "video")
    # áudio puro (.mka) no papel de áudio
    monkeypatch.setattr(merger, "ffprobe_json",
                        lambda p: {"streams": [{"codec_type": "audio"}]})
    jobs._probe_manual_file(af, "audio")


def test_happy_path(manual_env):
    vf, af, state = manual_env

    async def go():
        out = await jobs.create_manual(2, "pt", str(vf), str(af), None)
        jid = out["id"]
        assert out["mode"] == "files" and out["kind"] == "both"
        assert out["manual_files"] == {"video": str(vf), "audio": str(af)}
        assert sorted(state["probed"]) == [("audio.mkv", "audio"), ("video.mkv", "video")]
        await jobs._tasks[jid]
        from services import store
        db = store.get_job(jid)
        assert db["status"] == "done"
        assert db["movie"]["original_title"] == "Ex Machina"
        assert state["merged"] == [(str(vf), str(af), False)]
        assert jid not in jobs._jobs  # terminal: sai da memória
    asyncio.run(go())


def test_drift_pauses_and_proceed_resumes(manual_env, monkeypatch):
    vf, af, state = manual_env

    async def fake_merge_drift(job, video_file, audio_file, allow_drift=False):
        if not allow_drift:
            raise merger.VersionMismatch(-100.0, 700.0)
        state["merged"].append(("retomado", allow_drift))
        jobs._set(job, "done", "ok")
    monkeypatch.setattr(jobs, "_merge", fake_merge_drift)

    async def go():
        out = await jobs.create_manual(3, "pt", str(vf), str(af))
        jid = out["id"]
        await jobs._tasks[jid]
        job = jobs._jobs[jid]
        assert job["status"] == "awaiting" and job["drift_confirm"]["tau2_ms"] == 700.0
        assert await jobs.proceed(jid) is not None
        await jobs._tasks[jid]
        from services import store
        assert store.get_job(jid)["status"] == "done"
        assert state["merged"][-1] == ("retomado", True)
    asyncio.run(go())


def test_retry_recreates_manual(manual_env, monkeypatch):
    vf, af, state = manual_env

    async def fail(job, video_file, audio_file, allow_drift=False):
        raise RuntimeError("boom")
    monkeypatch.setattr(jobs, "_merge", fail)

    async def ok(job, video_file, audio_file, allow_drift=False):
        job["output"] = "/out/x.mkv"
        jobs._set(job, "done", "ok")

    async def go():
        from services import store
        out = await jobs.create_manual(4, "pt", str(vf), str(af))
        jid = out["id"]
        await jobs._tasks[jid]
        assert store.get_job(jid)["status"] == "error"
        monkeypatch.setattr(jobs, "_merge", ok)  # volta a funcionar
        new = await jobs.retry(jid)
        assert new is not None and new["manual_files"]["video"] == str(vf)
        assert new["mode"] == "files"
        await jobs._tasks[new["id"]]
        assert store.get_job(new["id"])["status"] == "done"
    asyncio.run(go())


def test_resume_pending(manual_env, monkeypatch):
    vf, af, _ = manual_env
    from services import store
    spawned = []

    async def fake_run_manual(job, v, a):
        spawned.append((job["id"], str(v), str(a)))
    monkeypatch.setattr(jobs, "_run_manual", fake_run_manual)

    base = {"tmdb_id": 5, "language": "pt", "mode": "files", "kind": "both",
            "status": "merging", "detail": "", "movie": None,
            "created_at": "2026-01-01T00:00:00", "video_torrent": None,
            "audio_torrent": None, "output": None,
            "progress": {"video": None, "audio": None},
            "search": None, "fallbacks": None, "current": None}
    j_ok = {**base, "id": "mm1", "manual_files": {"video": str(vf), "audio": str(af)}}
    j_gone = {**base, "id": "mm2",
              "manual_files": {"video": str(vf.parent / "sumiu.mkv"), "audio": str(af)}}
    j_wait = {**base, "id": "mm3", "status": "awaiting",
              "manual_files": {"video": str(vf), "audio": str(af)},
              "drift_confirm": {"video_file": str(vf), "audio_file": str(af),
                                "tau1_ms": 0, "tau2_ms": 500}}

    async def go():
        for j in (j_ok, j_gone, j_wait):
            jobs._jobs[j["id"]] = j
            store.upsert_job(j)
        jobs.resume_pending()
        await asyncio.gather(*jobs._tasks.values())
        assert spawned == [("mm1", str(vf), str(af))]
        assert store.get_job("mm2")["status"] == "error"
        assert jobs._jobs["mm3"]["status"] == "awaiting"  # pausa de drift intacta
    asyncio.run(go())


def test_cancel_manual_job_ignores_qbittorrent(manual_env, monkeypatch):
    vf, af, _ = manual_env
    from services import store

    class Boom:
        def __getattr__(self, name):
            raise AssertionError(f"qBittorrent chamado ({name}) num job manual!")
    monkeypatch.setattr(jobs, "_qbit", Boom())

    job = {"id": "mm9", "tmdb_id": 5, "language": "pt", "mode": "files", "kind": "both",
           "status": "awaiting", "detail": "", "movie": None,
           "created_at": "2026-01-01T00:00:00", "video_torrent": None,
           "audio_torrent": None, "output": None,
           "progress": {"video": None, "audio": None},
           "manual_files": {"video": str(vf), "audio": str(af)},
           "drift_confirm": {"video_file": str(vf), "audio_file": str(af),
                             "tau1_ms": 0, "tau2_ms": 500},
           "search": None, "fallbacks": None, "current": None}
    jobs._jobs["mm9"] = job
    store.upsert_job(job)

    async def go():
        out = await jobs.cancel("mm9", delete_torrents=True)
        assert out is not None and store.get_job("mm9")["status"] == "cancelled"
    asyncio.run(go())
