"""Entrega do resultado: merge, arquivo único e legendas externas.

Tudo o que escreve no destino final passa por aqui (sob a fila de conversão do
`runtime`), inclusive a limpeza dos torrents depois do sucesso.
"""

import asyncio
from datetime import datetime
from pathlib import Path

import config
from services import catalog, merger, transcode
from services.series import subs as ext_subs
from services.jobs import runtime
from services.jobs.runtime import (
    _event, _ffmpeg_hooks, _ffmpeg_procs, _has_torrents, _map_qbit_path,
    _register_proc, _set, _tag)


def _movie_output(job: dict) -> Path:
    movie = job["movie"]
    safe_title = catalog.safe_name(f"{movie['original_title']} ({movie['year']})")
    folder = catalog.folder_name(movie["original_title"], movie["year"], job.get("tmdb_id"))
    dest_dir = Path(job.get("destination_path") or config.OUTPUT_DIR)
    return dest_dir / folder / f"{safe_title} [{job['language']}+orig].mkv"


async def _deliver_single(job: dict, files: dict):
    """Job de um torrent só: entrega o arquivo direto no destino, sem merge.

    Com opções avançadas ativas, em vez do hardlink o arquivo único passa pela
    conversão (que por si só cai em hardlink se o plano inteiro der em cópia).
    """
    kind = "video" if "video" in files else "audio"
    src_file = files[kind]
    _event(job, "info", f"Arquivo baixado: {src_file}")

    movie = job["movie"]
    safe_title = catalog.safe_name(f"{movie['original_title']} ({movie['year']})")
    folder = catalog.folder_name(movie["original_title"], movie["year"], job.get("tmdb_id"))
    tag = "orig" if job["kind"] == "original" else job["language"]
    dest_dir = Path(job.get("destination_path") or config.OUTPUT_DIR)
    label = "original" if job["kind"] == "original" else f"dublado ({job['language']})"

    if job.get("convert"):
        opts = transcode.validate(job["convert"])
        output = dest_dir / folder / f"{safe_title} [{tag}].mkv"
        # registra o destino antes do ffmpeg: cancel() apaga o parcial (ver _merge)
        job["output"] = str(output)
        job["merge_started_at"] = datetime.now().isoformat(timespec="seconds")
        _set(job, "merging", f"Convertendo arquivo {label} ({src_file.name})...")

        log, on_progress = _ffmpeg_hooks(job)

        try:
            result = await asyncio.to_thread(
                transcode.convert_single, str(src_file), str(output), opts,
                job["language"], (movie or {}).get("original_language"),
                log=log, on_progress=on_progress, on_start=_register_proc(job["id"]))
        finally:
            _ffmpeg_procs.pop(job["id"], None)
        job["progress"]["merge"] = None
        job["output"] = result.output
        await _attach_external_subs(job, result.output, {kind: src_file},
                                    (0.0, 0.0) if kind == "video" else (None, 0.0), log,
                                    linked=result.linked)
        done_label = "sem conversão necessária" if result.linked else "convertido"
        _set(job, "done", f"Concluído ({label}, {done_label}): {result.output}")
        await _cleanup_torrents(job)
        return

    output = dest_dir / folder / f"{safe_title} [{tag}]{src_file.suffix}"
    job["merge_started_at"] = datetime.now().isoformat(timespec="seconds")
    _set(job, "merging", f"Entregando {label} no destino...")

    notes: list[str] = []
    # hardlink (fallback cópia) roda em thread para não travar a API em cópias grandes
    await asyncio.to_thread(merger._link_or_copy, src_file, output, notes)
    for n in notes:
        _event(job, "info", n)

    job["output"] = str(output)
    await _attach_external_subs(job, str(output), {kind: src_file},
                                (0.0, 0.0) if kind == "video" else (None, 0.0),
                                lambda m: _event(job, "merge", m), linked=True)
    _set(job, "done", f"Concluído ({label}): {output}")
    await _cleanup_torrents(job)


async def _attach_external_subs(job: dict, output: str, files: dict,
                                shifts, log, linked: bool = False):
    """Legendas externas dos torrents (.srt/.ass/.vtt) no arquivo entregue.

    files: {"video": Path, "audio": Path} (os que existirem); shifts: (s_video,
    s_audio) em segundos — tempo_saída = tempo_no_arquivo + shift; None = lado
    sem referência de tempo (não entra). Saída MKV recém-criada → mux; saída
    hardlinkada (ou não-MKV) → sidecars ao lado (muxar obrigaria a copiar o
    filme inteiro). Nunca derruba o job."""
    roots = job.get("src_roots") or {}

    def _find(kind: str) -> list[str]:
        f = files.get(kind)
        if f is None:
            return []
        root = roots.get(kind)
        try:
            root_p = _map_qbit_path(job, root) if root else Path(f).parent
        except Exception:  # noqa: BLE001
            root_p = Path(f).parent
        return [str(p) for p in ext_subs.find_for_movie(root_p, str(f))]

    try:
        v_subs, a_subs = await asyncio.gather(
            asyncio.to_thread(_find, "video"), asyncio.to_thread(_find, "audio"))
        if not v_subs and not a_subs:
            return
        movie = job.get("movie") or {}
        orig_lang = merger.canonical_lang(merger.LANG_ISO.get(
            movie.get("original_language") or "", movie.get("original_language") or "und"))
        dub_lang = merger.canonical_lang(merger.LANG_ISO.get(job["language"], job["language"]))
        # job de um torrent só: um "kind" pode ser dublado ou original
        v_lang = dub_lang if job.get("kind") == "dubbed" and "audio" not in files else orig_lang
        a_lang = dub_lang
        mode = "sidecar" if (linked or Path(output).suffix.lower() != ".mkv") else "mux"
        n = await asyncio.to_thread(
            ext_subs.attach, output, v_subs, a_subs,
            None if shifts[0] is None else ext_subs.shift_fn(shifts[0]),
            None if shifts[1] is None else ext_subs.shift_fn(shifts[1]),
            str(files.get("video") or ""), str(files.get("audio") or ""),
            v_lang, a_lang, log, mode)
        if n:
            _event(job, "merge", f"{n} legenda(s) externa(s) do torrent "
                                 f"{'gravada(s) ao lado' if mode == 'sidecar' else 'anexada(s)'}")
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001
        _event(job, "merge", f"⚠️ legendas externas não anexadas ({e})")


async def _merge(job: dict, video_file: Path, audio_file: Path,
                 allow_drift: bool = False):
    _event(job, "merge", f"Arquivo de vídeo: {video_file}")
    _event(job, "merge", f"Arquivo de áudio: {audio_file}")

    movie = job["movie"]
    safe_title = catalog.safe_name(f"{movie['original_title']} ({movie['year']})")
    # subpasta por filme dentro do destino escolhido (bom para Jellyfin/Plex)
    folder = catalog.folder_name(movie["original_title"], movie["year"], job.get("tmdb_id"))
    dest_dir = Path(job.get("destination_path") or config.OUTPUT_DIR)
    output = dest_dir / folder / f"{safe_title} [{job['language']}+orig].mkv"
    # registra o destino JÁ: se o usuário cancelar durante o ffmpeg, o cancel()
    # precisa saber qual arquivo parcial apagar
    job["output"] = str(output)

    job["merge_started_at"] = datetime.now().isoformat(timespec="seconds")
    _set(job, "merging", f"Fazendo merge ({video_file.name} + {audio_file.name})...")

    log, on_progress = _ffmpeg_hooks(job)

    # opções avançadas (se o job tiver): re-valida contra o servidor atual
    convert_opts = transcode.validate(job["convert"]) if job.get("convert") else None

    # merger.merge é bloqueante (ffmpeg/ffprobe); roda em thread para não travar a API
    try:
        result = await asyncio.to_thread(
            merger.merge, str(video_file), str(audio_file), str(output),
            job["language"], log=log, on_progress=on_progress,
            allow_drift=allow_drift, convert=convert_opts,
            original_lang=(job.get("movie") or {}).get("original_language"),
            on_start=_register_proc(job["id"]))
    finally:
        _ffmpeg_procs.pop(job["id"], None)
    job["progress"]["merge"] = None  # terminou (com sucesso): some a barra

    job["output"] = result.output
    if result.linked:
        ref = result.ref_input if result.ref_input is not None else 0
        shifts = (0.0 if ref == 0 else None, 0.0 if ref == 1 else None)
    else:
        shifts = result.input_shifts
    await _attach_external_subs(
        job, result.output, {"video": video_file, "audio": audio_file}, shifts, log,
        linked=result.linked)
    if result.linked:
        _set(job, "done", f"Áudio no idioma alvo já existia no melhor vídeo — hardlink criado: {result.output}")
    else:
        # None quando o merge virou conversão de arquivo único (o melhor vídeo já
        # tinha o áudio alvo): não houve alinhamento, não há offset
        sync = (f" (offset {result.offset_ms:+.2f} ms)"
                if result.offset_ms is not None else "")
        _set(job, "done", f"Concluído{sync}: {result.output}")

    await _cleanup_torrents(job)


async def _cleanup_torrents(job: dict):
    if not _has_torrents(job):
        return  # conversão manual / recompressão: não há torrents para limpar
    if config.QBIT_CLEANUP == "keep":
        return
    # hardlink/cópia são independentes do arquivo do qBittorrent (nada de symlink),
    # então remove_data pode apagar os dados com segurança mesmo quando só linkou.
    delete_files = config.QBIT_CLEANUP == "remove_data"
    for kind in ("video", "audio"):
        try:
            await runtime._qbit.delete_by_tag(_tag(job, kind), delete_files)
            _event(job, "qbit", f"Torrent de {kind} removido do qBittorrent"
                                + (" (com os dados)" if delete_files else ""))
        except Exception as e:  # noqa: BLE001
            _event(job, "qbit", f"Falha ao remover torrent de {kind}: {e}")
