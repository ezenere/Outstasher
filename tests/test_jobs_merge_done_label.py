"""Rótulo final do merge quando NÃO houve offset a aplicar.

Se o melhor vídeo já tem o áudio no idioma alvo e o job tem opções avançadas, o
merger pula o alinhamento e delega para transcode.convert_single: o resultado
volta convertido (linked=False) mas SEM offset_ms. Formatar esse None como
"{:+.2f}" quebrava o job no fim — depois de horas de encode, com o arquivo já
pronto no disco.
"""
import asyncio
from pathlib import Path

from services import jobs, merger


def _run(temp_db, monkeypatch, result):
    job = {
        "id": "j1", "tmdb_id": 62, "language": "pt", "kind": "both",
        "status": "merging", "created_at": "2026-07-29T21:55:00",
        "detail": "", "progress": {}, "convert": None,
        "movie": {"original_title": "2001 A Space Odyssey", "year": "1968"},
        "destination_path": str(Path(temp_db.__file__).parent),  # qualquer dir
    }
    jobs._jobs[job["id"]] = job

    async def fake_to_thread(fn, *a, **k):
        return result

    monkeypatch.setattr(jobs.asyncio, "to_thread", fake_to_thread)

    async def noop(_job):
        return None
    monkeypatch.setattr(jobs, "_cleanup_torrents", noop)

    asyncio.run(jobs._merge(job, Path("v.mkv"), Path("a.mkv")))
    return job


def test_convertido_sem_offset(temp_db, monkeypatch):
    """convert_single via atalho de idioma: sem offset, mas convertido."""
    r = merger.MergeResult(output="/out/filme.mkv")
    r.linked = False
    r.offset_ms = None  # nunca foi calculado (nao houve alinhamento)
    job = _run(temp_db, monkeypatch, r)
    assert job["status"] == "done"
    assert "offset" not in job["detail"]        # nao promete um offset que nao existe
    assert "/out/filme.mkv" in job["detail"]


def test_com_offset_mostra_o_valor(temp_db, monkeypatch):
    r = merger.MergeResult(output="/out/filme.mkv")
    r.linked = False
    r.offset_ms = -1234.56
    job = _run(temp_db, monkeypatch, r)
    assert job["status"] == "done"
    assert "-1234.56 ms" in job["detail"]


def test_hardlink_tem_rotulo_proprio(temp_db, monkeypatch):
    r = merger.MergeResult(output="/out/filme.mkv")
    r.linked = True
    job = _run(temp_db, monkeypatch, r)
    assert job["status"] == "done"
    assert "hardlink" in job["detail"]
