"""Pausa de drift em FILMES: perfil do offset em várias janelas, opção de
alinhamento avançado (o alinhador das séries) e o gate de revisão no job de
filme (mesmo formato de decisão da UI das séries)."""
import asyncio

import pytest

from services import jobs, merger, store
from services import tmdb


@pytest.mark.parametrize("offs,expected", [
    ([0.0, 0.01, 0.02, 0.03], "flat"),                       # tudo dentro de 50 ms
    ([0.0, 0.06, 0.12, 0.18, 0.24, 0.30], "drift"),          # cresce aos poucos
    ([0.0, 0.0, 0.01, 0.50, 0.50, 0.51], "cut"),             # salto entre patamares
    ([0.0, 0.30, 0.10, 0.45, 0.02, 0.60], "mixed"),          # vai e volta
    ([0.0, 0.5], "unknown"),                                 # poucas janelas
])
def test_shape_verdict(offs, expected):
    assert jobs._shape_verdict(offs) == expected


@pytest.fixture
def drift_env(temp_db, monkeypatch, tmp_path):
    vf, af = tmp_path / "video.mkv", tmp_path / "audio.mkv"
    vf.write_bytes(b"x")
    af.write_bytes(b"x")
    temp_db.add_destination("Filmes", str(tmp_path / "out"), True)
    monkeypatch.setattr(jobs.movies, "_probe_manual_file", lambda p, r: None)

    async def fake_details(tmdb_id, lang):
        return {"original_title": "Filme Exemplo", "localized_title": "Filme Exemplo",
                "year": "2014", "poster": None}
    monkeypatch.setattr(tmdb, "details", fake_details)

    async def merge_drift(job, video_file, audio_file, allow_drift=False):
        raise merger.VersionMismatch(-100.0, 700.0)
    monkeypatch.setattr(jobs.delivery, "_merge", merge_drift)
    # perfil: 5 janelas com salto no meio (corte)
    monkeypatch.setattr(jobs.advanced, "_offset_profile", lambda v, a: (
        [{"t": 30 + 300 * i, "offset_ms": o, "quality": 50.0}
         for i, o in enumerate([-100, -100, -98, 700, 702])], "cut"))
    return vf, af


def test_drift_mede_perfil_e_oferece_avancado(drift_env, monkeypatch):
    vf, af = drift_env
    ran = {}

    async def fake_advanced(job, video_file, audio_file):
        ran["files"] = (str(video_file), str(audio_file))
        job["output"] = "/out/x.mkv"
        jobs._set(job, "done", "ok")
    monkeypatch.setattr(jobs.advanced, "_run_advanced", fake_advanced)

    async def go():
        out = await jobs.create_manual(3, "pt", str(vf), str(af))
        jid = out["id"]
        await jobs._tasks[jid]
        job = jobs._jobs[jid]
        assert job["status"] == "awaiting"
        dc = job["drift_confirm"]
        assert dc["verdict"] == "cut" and len(dc["profile"]) == 5
        # o perfil aparece no público (UI) e no banco
        assert store.get_job(jid)["drift_confirm"]["verdict"] == "cut"
        assert await jobs.proceed(jid, "advanced") is not None
        await jobs._tasks[jid]
        assert ran["files"] == (str(vf), str(af))
        assert store.get_job(jid)["status"] == "done"
    asyncio.run(go())


def _review_job(vf, af, needs_review=True):
    edl = {
        "version": 1, "episode": "filme",
        "source_dub": {"path": str(af), "duration": 100.0},
        "source_orig": {"path": str(vf), "duration": 100.0},
        "segments": [
            {"kind": "match", "a_start": 0.0, "a_end": 50.0, "b_start": 0.0,
             "b_end": 50.0, "offset": 0.0, "residual": 3.0, "confidence": 0.9,
             "slope": 1.0, "note": ""},
            {"kind": "replaced", "a_start": 50.0, "a_end": 60.0,
             "b_start": 50.0, "b_end": 60.0, "offset": None, "residual": 30.0,
             "confidence": 0.0, "slope": None, "note": "revisar"},
        ],
        "review": {"required": needs_review, "flagged": [
            {"a_start": 50.0, "a_end": 60.0, "reason": "replaced"}]},
    }
    return {
        "id": "adv1", "tmdb_id": 5, "language": "pt", "mode": "files", "kind": "both",
        "status": "awaiting", "detail": "", "movie": {"original_title": "Filme Exemplo",
                                                     "year": "2014"},
        "created_at": "2026-01-01T00:00:00", "video_torrent": None,
        "audio_torrent": None, "output": None,
        "progress": {"video": None, "audio": None},
        "manual_files": {"video": str(vf), "audio": str(af)},
        "advanced": {"video_file": str(vf), "audio_file": str(af), "edl": edl},
        "awaiting": {"reason": "alignment_review", "payload": {"episodes": {"filme": edl}}},
        "search": None, "fallbacks": None, "current": None,
    }


def test_resolve_review_filme_renderiza(drift_env, monkeypatch):
    vf, af = drift_env
    job = _review_job(vf, af)
    jobs._jobs["adv1"] = job
    store.upsert_job(job)
    rendered = {}

    async def fake_render(j):
        rendered["edl"] = j["advanced"]["edl"]
        jobs._set(j, "done", "ok")
    monkeypatch.setattr(jobs.advanced, "_render_advanced", fake_render)

    async def go():
        # ação inválida é recusada
        with pytest.raises(ValueError):
            await jobs.resolve_review("adv1", "alignment_review",
                                      {"actions": {"filme": {"1": "explodir"}}})
        # sem decisão: continua aguardando
        out = await jobs.resolve_review("adv1", "alignment_review", {})
        assert out["status"] == "awaiting"
        # decisão explícita resolve e renderiza
        out = await jobs.resolve_review("adv1", "alignment_review",
                                        {"actions": {"filme": {"1": "fill_original"}}})
        await jobs._tasks["adv1"]
        assert rendered["edl"]["segments"][1]["action"] == "fill_original"
        assert store.get_job("adv1")["status"] == "done"
    asyncio.run(go())


def test_resolve_review_filme_skip_falha(drift_env):
    vf, af = drift_env
    job = _review_job(vf, af)
    jobs._jobs["adv1"] = job
    store.upsert_job(job)

    async def go():
        out = await jobs.resolve_review("adv1", "alignment_review", {"skip": ["filme"]})
        assert out["status"] == "error"
        assert "pulada" in (store.get_job("adv1")["detail"] or "")
    asyncio.run(go())


def test_decisao_durante_a_medicao_nao_e_atropelada(drift_env, monkeypatch):
    """Caso real de campo: o usuário deu proceed ENQUANTO o perfil ainda era
    medido. Ao terminar, o handler reescrevia o status para 'awaiting' por
    cima do alinhamento em andamento — e a UI aceitava um SEGUNDO proceed."""
    vf, af = drift_env
    import threading
    liberar = threading.Event()

    def perfil_lento(v, a):
        # bloqueia a thread até o teste liberar (simula os 25 min de janelas)
        liberar.wait(timeout=10)
        return ([{"t": 30, "offset_ms": -100, "quality": 50.0}] * 3, "cut")
    monkeypatch.setattr(jobs.advanced, "_offset_profile", perfil_lento)

    perfil_ok = asyncio.Event()

    async def fake_advanced(job, video_file, audio_file):
        jobs._set(job, "merging", "Alinhando por conteúdo...")
        # segura o "alinhamento" até o handler do perfil ter a chance de
        # atropelar o status (é o que este teste pune)
        liberar.set()
        await asyncio.wait_for(perfil_ok.wait(), 5)
        await asyncio.sleep(0.05)
    monkeypatch.setattr(jobs.advanced, "_run_advanced", fake_advanced)

    async def go():
        out = await jobs.create_manual(3, "pt", str(vf), str(af))
        jid = out["id"]
        job = jobs._jobs[jid]
        pausa = jobs._tasks[jid]     # a task da pausa (medindo o perfil)
        try:
            for _ in range(300):     # espera o gate abrir
                if job["status"] == "awaiting" and job.get("drift_confirm"):
                    break
                await asyncio.sleep(0.01)
            assert job["status"] == "awaiting"
            # usuário decide DURANTE a medição
            assert await jobs.proceed(jid, "advanced") is not None
            await pausa              # medição termina (e tenta atropelar)
            perfil_ok.set()
            await jobs._tasks[jid]   # o "alinhamento" conclui
            assert job["status"] != "awaiting", (job["status"], job["detail"])
            # e um segundo proceed é recusado (gate já consumido)
            assert await jobs.proceed(jid, "advanced") is None
        finally:
            liberar.set()
            perfil_ok.set()
    asyncio.run(go())
