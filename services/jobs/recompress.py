"""Recompressão de um filme que já está na coleção (sem torrent)."""

import asyncio
import uuid
from datetime import datetime
from pathlib import Path

from services import catalog, store, tmdb, transcode
from services.jobs.runtime import (
    _event, _fail, _ffmpeg_hooks, _ffmpeg_procs, _free_name, _get_merge_lock,
    _jobs, _public, _register_proc, _set, _spawn)


async def create_recompress(destination_id: int | None, folder: str, rel: str,
                            convert: dict, replace: bool = True,
                            tmdb_id: int | None = None) -> dict:
    """Recomprime um arquivo que JÁ ESTÁ na coleção, no lugar.

    Sem download e sem merge: só o convert_single com as opções avançadas. A
    saída vai para um arquivo temporário na mesma pasta; com replace=True o
    original só é trocado quando o ffmpeg termina — cancelar/falhar deixa o
    filme intacto (ver _run_recompress).
    """
    # o preparo bloqueia: media_path/stat batem no disco (destinos podem ser
    # montagens de rede) e validate() com HW dispara um test-encode de ffmpeg.
    # Sai do event loop para não travar o polling de todos os clientes.
    def _prepare():
        src = catalog.media_path(destination_id, folder, rel)
        opts = transcode.validate(convert)
        if opts.is_noop():
            raise ValueError("Nenhuma opção de conversão escolhida — não há o que recomprimir")
        dest = store.get_destination(destination_id) if destination_id \
            else store.default_destination()
        if dest is None:
            raise ValueError("Nenhum destino cadastrado")
        return src, opts, dest

    src, opts, dest = await asyncio.to_thread(_prepare)

    job = {
        "id": uuid.uuid4().hex[:10],
        "tmdb_id": tmdb_id or catalog.tmdb_id_in(folder),
        # recompressão não troca idioma nem baixa torrents: sem kind, e o idioma
        # serve só para buscar os metadados do TMDB (capa/descrição do detalhe)
        "language": "pt",
        "mode": "recompress",
        "kind": None,
        "convert": opts.to_dict(),
        "status": "merging",
        "detail": "Preparando recompressão...",
        "movie": None,
        "video_torrent": None,
        "audio_torrent": None,
        "progress": {"video": None, "audio": None},
        "output": None,
        "destination_id": dest["id"],
        "destination_label": dest["label"],
        "destination_path": dest["path"],
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "recompress": {"folder": folder, "rel": rel, "replace": bool(replace)},
        "search": None,
        "fallbacks": None,
        "current": None,
    }
    _jobs[job["id"]] = job
    conv = transcode.describe(opts)
    mode_label = "substituindo o original" if replace else "mantendo o original"
    _event(job, "status", f"Recompressão criada ({mode_label}) — {folder}/{rel}"
                          + (f" — conversão: {', '.join(conv)}" if conv else ""))
    _spawn(job["id"], _run_recompress(job, src, opts))
    return _public(job)


async def _run_recompress(job: dict, src: Path, opts: "transcode.ConvertOptions | None" = None):
    """Recompressão: TMDB (só para a capa/descrição) -> fila -> convert_single.

    `opts` já validado vem do create; no resume (job relido do banco) é None e
    a validação roda uma vez aqui."""
    if opts is None:
        opts = transcode.validate(job["convert"])
    # .tmp na MESMA pasta: rename atômico no fim e sem cópia entre discos
    tmp = src.with_name(f".{src.stem}.recompress-{job['id']}.mkv")
    try:
        if job.get("tmdb_id"):
            try:
                job["movie"] = await tmdb.details(job["tmdb_id"], job["language"])
            except Exception:  # noqa: BLE001 - metadado é cosmético aqui
                pass
        lock = _get_merge_lock()
        if lock.locked():
            _set(job, "merging", "Na fila de conversão...")
        async with lock:
            await _recompress(job, src, tmp, opts)
    except asyncio.CancelledError:
        tmp.unlink(missing_ok=True)  # cancelado: nada de lixo (o original nem foi tocado)
        raise
    except Exception as e:  # noqa: BLE001
        tmp.unlink(missing_ok=True)
        _fail(job, f"{type(e).__name__}: {e}")


async def _recompress(job: dict, src: Path, tmp: Path, opts: "transcode.ConvertOptions"):
    movie = job.get("movie") or {}
    before = src.stat().st_size
    # o parcial é o .tmp: o cancel() apaga ele, nunca o filme original
    job["output"] = str(tmp)
    job["merge_started_at"] = datetime.now().isoformat(timespec="seconds")
    _set(job, "merging", f"Recomprimindo {src.name}...")

    log, on_progress = _ffmpeg_hooks(job)

    try:
        result = await asyncio.to_thread(
            transcode.convert_single, str(src), str(tmp), opts,
            None, movie.get("original_language"),
            log=log, on_progress=on_progress, on_start=_register_proc(job["id"]))
    finally:
        _ffmpeg_procs.pop(job["id"], None)
    job["progress"]["merge"] = None

    out = Path(result.output)
    if result.linked:
        # o plano inteiro virou cópia: o hardlink do convert_single não tem
        # valor aqui (a origem é o próprio filme) — nada mudou no disco
        await asyncio.to_thread(out.unlink, missing_ok=True)
        job["output"] = str(src)
        _set(job, "done", "Nada a recomprimir com as opções escolhidas — arquivo mantido")
        return

    replace = job["recompress"]["replace"]
    # a finalização mexe no disco (stat/unlink/replace) — em destino de rede
    # cada op custa; agrupa num só to_thread para não travar o event loop
    def _finalize() -> tuple[str, int, str | None]:
        after = out.stat().st_size
        if after >= before:  # ficou MAIOR: recomprimir para inflar é inútil
            out.unlink(missing_ok=True)
            return str(src), after, None
        if replace:
            out.replace(src)  # atômico: o filme nunca some, só troca de conteúdo
            return str(src), after, str(src)
        final = _free_name(src.with_name(f"{src.stem} [recomprimido].mkv"))
        out.replace(final)
        return str(final), after, str(final)

    final_path, after, wrote = await asyncio.to_thread(_finalize)
    job["output"] = final_path
    if wrote is None:
        _set(job, "done", f"Recompressão descartada: o resultado ficou maior "
                          f"({catalog.human_size(after)} vs {catalog.human_size(before)}) — "
                          f"arquivo original mantido")
        return
    saved = before - after
    _set(job, "done", f"Recomprimido: {catalog.human_size(before)} → "
                      f"{catalog.human_size(after)} "
                      f"(-{saved * 100 // before}%, {catalog.human_size(saved)} livres) — "
                      f"{Path(final_path).name}")
    catalog.invalidate_library()
