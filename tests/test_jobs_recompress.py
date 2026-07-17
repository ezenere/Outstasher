"""Recompressão de um filme da coleção (jobs.create_recompress).

O ponto sensível é a segurança do arquivo do usuário: o original só pode sumir
quando a conversão termina bem. Os testes marcados com `ffmpeg` precisam do
ffmpeg real (o .mkv de origem vem do make_media, e create_recompress valida os
codecs contra os encoders instalados). A maioria roda o convert_single de
verdade e observa o disco; os que testam o tratamento de FALHA/segurança
substituem só o convert_single por um stub que explode ou infla, para provocar
o caminho de erro sem depender de uma opção que o encoder recuse.
"""
import asyncio

import pytest

from services import catalog, jobs

# h264 640x272 a 500k (o make_media) -> pedir 200k em HEVC é uma redução real
CONV = {"video_codec": "hevc", "video_bitrate": 200}


@pytest.fixture
def library(temp_db, tmp_path, make_media):
    """Destino com 'Filme (2020)/filme.mkv'. Devolve (dest_id, folder, arquivo)."""
    root = tmp_path / "colecao"
    item = root / "Filme (2020)"
    item.mkdir(parents=True)
    dest = temp_db.add_destination("T", str(root), True)
    src = item / "filme.mkv"
    make_media(src, ["eng"], w=640, h=272, dur=3)
    return dest["id"], "Filme (2020)", src


async def _wait(job_id: str, timeout=180):
    """Espera a task do job terminar (o pipeline roda em background)."""
    task = jobs._tasks.get(job_id)
    if task:
        await asyncio.wait_for(asyncio.shield(task), timeout)


def test_rejects_no_op_options(library):
    did, folder, _src = library
    async def go():
        with pytest.raises(ValueError, match="Nenhuma opção"):
            await jobs.create_recompress(did, folder, "filme.mkv", {})
    asyncio.run(go())


def test_rejects_non_video(library, tmp_path):
    did, folder, src = library
    (src.parent / "nota.txt").write_bytes(b"x")
    async def go():
        with pytest.raises(catalog.CatalogError):
            await jobs.create_recompress(did, folder, "nota.txt", CONV)
    asyncio.run(go())


def test_job_shape(library, monkeypatch):
    """O job nasce com o modo/metadados certos (sem rodar o ffmpeg)."""
    did, folder, src = library
    async def _noop(job, src, opts=None):
        return None
    monkeypatch.setattr(jobs, "_run_recompress", _noop)

    async def go():
        job = await jobs.create_recompress(did, folder, "filme.mkv", CONV, replace=False)
        full = jobs._jobs[job["id"]]
        assert full["mode"] == "recompress"
        assert full["recompress"]["folder"] == folder
        assert full["recompress"]["rel"] == "filme.mkv"
        assert full["recompress"]["replace"] is False
        assert full["convert"]["video_codec"] == "hevc"
        # não é job de torrent: nada de busca/candidatos/kind
        assert full["search"] is None and full["video_torrent"] is None
        assert full["kind"] is None
    asyncio.run(go())


def test_tmdb_id_from_folder(library, monkeypatch):
    """Pasta marcada com [tmdbid-N]: o job herda o id sem precisar do frontend."""
    did, _folder, src = library
    tagged = src.parent.with_name("Filme (2020) [tmdbid-603]")
    src.parent.rename(tagged)
    async def _noop(job, src, opts=None):
        return None
    monkeypatch.setattr(jobs, "_run_recompress", _noop)

    async def go():
        job = await jobs.create_recompress(did, tagged.name, "filme.mkv", CONV)
        assert jobs._jobs[job["id"]]["tmdb_id"] == 603
    asyncio.run(go())


@pytest.mark.ffmpeg
def test_replace_swaps_only_on_success(library):
    did, folder, src = library
    before = src.read_bytes()

    async def go():
        job = await jobs.create_recompress(did, folder, "filme.mkv", CONV, replace=True)
        await _wait(job["id"])
        return jobs._lookup(job["id"])

    job = asyncio.run(go())
    assert job["status"] == "done", job["detail"]
    assert src.is_file(), "o filme sumiu do lugar"
    assert src.read_bytes() != before, "o arquivo não foi trocado pelo convertido"
    assert job["output"] == str(src)  # substituiu no lugar
    # nenhum .tmp sobrou na pasta
    assert not [p for p in src.parent.iterdir() if p.name.startswith(".")]


@pytest.mark.ffmpeg
def test_keep_both_writes_alongside(library):
    did, folder, src = library
    before = src.read_bytes()

    async def go():
        job = await jobs.create_recompress(did, folder, "filme.mkv", CONV, replace=False)
        await _wait(job["id"])
        return jobs._lookup(job["id"])

    job = asyncio.run(go())
    assert job["status"] == "done", job["detail"]
    assert src.read_bytes() == before, "o original foi alterado no modo 'manter os dois'"
    out = src.parent / "filme [recomprimido].mkv"
    assert out.is_file() and job["output"] == str(out)


@pytest.mark.ffmpeg
def test_failure_keeps_original(library, monkeypatch):
    """ffmpeg falhando (opções impossíveis) não pode encostar no filme."""
    did, folder, src = library
    before = src.read_bytes()

    def boom(*a, **k):
        raise RuntimeError("ffmpeg explodiu")
    monkeypatch.setattr(jobs.transcode, "convert_single", boom)

    async def go():
        job = await jobs.create_recompress(did, folder, "filme.mkv", CONV)
        await _wait(job["id"])
        return jobs._lookup(job["id"])

    job = asyncio.run(go())
    assert job["status"] == "error"
    assert src.read_bytes() == before, "o original mudou apesar da falha"
    assert not [p for p in src.parent.iterdir() if p.name.startswith(".")]  # sem lixo


@pytest.mark.ffmpeg
def test_nothing_to_do_keeps_file(library):
    """Bitrate pedido acima do da fonte: a regra 'nunca converter para cima' do
    planner mantém o vídeo, e a recompressão não mexe no arquivo."""
    did, folder, src = library
    before = src.read_bytes()

    async def go():
        job = await jobs.create_recompress(
            did, folder, "filme.mkv",
            {"video_codec": "hevc", "video_bitrate": 100_000, "preset": "veryfast"})
        await _wait(job["id"])
        return jobs._lookup(job["id"])

    job = asyncio.run(go())
    assert job["status"] == "done"
    assert "Nada a recomprimir" in job["detail"], job["detail"]
    assert src.read_bytes() == before, "o arquivo mudou sem haver o que converter"
    assert job["output"] == str(src)
    # o hardlink que o convert_single cria nesse caso não pode sobrar na pasta
    assert sorted(p.name for p in src.parent.iterdir()) == ["filme.mkv"]


@pytest.mark.ffmpeg
def test_discards_when_result_is_bigger(library, monkeypatch):
    """Se a conversão roda e o resultado fica MAIOR, descarta e mantém o
    original (recomprimir para inflar é o oposto do objetivo)."""
    did, folder, src = library
    before = src.read_bytes()
    real = jobs.transcode.convert_single

    def fake(src_p, out_p, opts, *a, **k):
        r = real(src_p, out_p, opts, *a, **k)
        # finge um encode ruim: infla a saída acima do original
        from pathlib import Path as _P
        _P(r.output).write_bytes(b"x" * (len(before) + 1024))
        return r
    monkeypatch.setattr(jobs.transcode, "convert_single", fake)

    async def go():
        job = await jobs.create_recompress(did, folder, "filme.mkv", CONV)
        await _wait(job["id"])
        return jobs._lookup(job["id"])

    job = asyncio.run(go())
    assert job["status"] == "done"
    assert "maior" in job["detail"], job["detail"]
    assert src.read_bytes() == before, "o original foi trocado por um arquivo maior"
    assert job["output"] == str(src)
    assert sorted(p.name for p in src.parent.iterdir()) == ["filme.mkv"]


@pytest.mark.ffmpeg
def test_retry_recreates_recompress(library, monkeypatch):
    did, folder, _src = library
    async def _noop(job, src, opts=None):
        return None
    monkeypatch.setattr(jobs, "_run_recompress", _noop)

    async def go():
        job = await jobs.create_recompress(did, folder, "filme.mkv", CONV, replace=False)
        old = dict(jobs._jobs[job["id"]])
        old["status"] = "error"
        jobs.store.upsert_job(old)
        jobs._jobs.pop(old["id"], None)

        novo = await jobs.retry(old["id"])
        full = jobs._jobs[novo["id"]]
        assert full["mode"] == "recompress"
        assert full["recompress"]["rel"] == "filme.mkv"
        assert full["recompress"]["replace"] is False  # a escolha do usuário sobrevive
    asyncio.run(go())


def test_resume_after_restart_uses_recompress_pipeline(library, monkeypatch):
    """Job de recompressão interrompido por restart tem de voltar para
    _run_recompress — NUNCA para _run_from_download (que procuraria torrents
    inexistentes e deixaria o .tmp órfão)."""
    did, folder, src = library
    routed = {}

    async def fake_recompress(job, s, opts=None):
        routed["recompress"] = (job["id"], str(s))

    async def fake_download(job):
        routed["download"] = job["id"]  # nunca deve ser chamado

    monkeypatch.setattr(jobs, "_run_recompress", fake_recompress)
    monkeypatch.setattr(jobs, "_run_from_download", fake_download)

    async def go():
        # job de recompressão como fica no banco após restart (status ativo,
        # sem manual_files, kind None) — recriado direto para simular o reload
        job = {"id": "rc1", "tmdb_id": None, "language": "pt", "mode": "recompress",
               "kind": None, "convert": CONV, "status": "merging", "detail": "",
               "movie": None, "video_torrent": None, "audio_torrent": None,
               "output": None, "destination_id": did,
               "created_at": "2026-01-01T00:00:00",
               "progress": {"video": None, "audio": None},
               "recompress": {"folder": folder, "rel": "filme.mkv", "replace": True},
               "search": None, "fallbacks": None, "current": None}
        jobs._jobs["rc1"] = job
        jobs.resume_pending()
        await asyncio.gather(*jobs._tasks.values())
        assert routed.get("recompress") == ("rc1", str(src))
        assert "download" not in routed  # não caiu no pipeline de torrents
    asyncio.run(go())


def test_resume_missing_source_errors(library, monkeypatch):
    """Se o filme sumiu do disco entre o restart e o resume, o job vai a erro
    em vez de tentar rodar sobre um arquivo inexistente."""
    did, folder, _src = library
    monkeypatch.setattr(jobs, "_run_recompress",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("não deveria rodar")))

    async def go():
        job = {"id": "rc2", "tmdb_id": None, "language": "pt", "mode": "recompress",
               "kind": None, "convert": CONV, "status": "merging", "detail": "",
               "movie": None, "video_torrent": None, "audio_torrent": None,
               "output": None, "destination_id": did,
               "created_at": "2026-01-01T00:00:00",
               "progress": {"video": None, "audio": None},
               "recompress": {"folder": folder, "rel": "sumiu.mkv", "replace": True},
               "search": None, "fallbacks": None, "current": None}
        jobs._jobs["rc2"] = job
        jobs.resume_pending()
        assert jobs._lookup("rc2")["status"] == "error"
    asyncio.run(go())
