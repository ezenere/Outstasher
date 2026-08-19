"""Etapas da conversão: quem rotula é o backend, e o rótulo é o MESMO no
dropdown (summary), no card da lista (_slim_job) e no detalhe (progress)."""
from services import jobs
from services.jobs import runtime, views


def _job(phase=None, pct=42.0):
    prog = {"video": None, "audio": None, "merge": None}
    if phase is not None:
        prog["merge"] = {"pct": pct, "phase": phase, "out_s": 1.0,
                         "duration_s": 2.0, "speed": 1.0, "fps": 1.0,
                         "size": 0, "bitrate": 0, "eta": 0}
    return {"id": "ph1", "tmdb_id": 7, "language": "pt", "mode": "auto",
            "kind": "both", "status": "merging", "detail": "", "progress": prog,
            "movie": {"original_title": "Filme Exemplo", "year": "2014"},
            "created_at": "2026-01-01T00:00:00", "output": None,
            "video_torrent": None, "audio_torrent": None, "search": None,
            "fallbacks": None, "current": None}


def test_fase_vai_para_o_dropdown_e_para_o_card(temp_db):
    job = _job(runtime.PHASE_ALIGN)
    jobs._jobs["ph1"] = job
    resumo = next(x for x in jobs.summary() if x["id"] == "ph1")
    assert resumo["state"] == "converting" and resumo["phase"] == "align"
    assert views._slim_job(job)["progress"]["merge_phase"] == "align"

    # sem rótulo (jobs antigos, merge de filme comum) = conversão mesmo
    job["progress"]["merge"] = {"pct": 10.0}
    assert views._slim_job(job)["progress"]["merge_phase"] == "convert"
    # e fora do merging não há etapa nenhuma
    job["status"] = "downloading"
    assert views._slim_job(job)["progress"]["merge_phase"] is None


def test_hooks_carimbam_a_etapa(temp_db):
    """_ffmpeg_hooks(job, fase) carimba TODO progresso daquela etapa — é isso
    que faz o dropdown/lista/detalhe dizerem a mesma coisa sem adivinhar."""
    job = _job()
    jobs._jobs["ph1"] = job
    _log, on_progress = runtime._ffmpeg_hooks(job, runtime.PHASE_EDL)
    on_progress({"pct": 3.0})
    assert job["progress"]["merge"] == {"pct": 3.0, "phase": "edl"}

    # o padrão é conversão de verdade
    _log2, on_progress2 = runtime._ffmpeg_hooks(job)
    on_progress2({"pct": 9.0})
    assert job["progress"]["merge"]["phase"] == runtime.PHASE_CONVERT

    # quem já manda a fase no info (fingerprint) manda mesmo
    on_progress2({"pct": 1.0, "phase": runtime.PHASE_ALIGN, "step": 1})
    assert job["progress"]["merge"]["phase"] == "align"
