"""Pipeline dos jobs de filme: criação e execução de ponta a ponta.

busca -> (escolha manual) -> download -> merge/entrega. Também cria os jobs de
conversão manual (dois arquivos locais).
"""

import asyncio
import uuid
from datetime import datetime
from pathlib import Path

from services import merger, store, tmdb, transcode
from services.jobs import advanced, delivery, downloads, search
from services.jobs.runtime import (
    KINDS, _event, _fail, _get_merge_lock, _jobs, _needed_torrents, _public,
    _set, _spawn)


async def create(tmdb_id: int, language: str, mode: str = "auto",
                 destination_id: int | None = None,
                 torrent_target_id: int | None = None,
                 kind: str = "both", download_only: bool = False,
                 convert: dict | None = None) -> dict:
    if kind not in KINDS:
        raise ValueError(f"kind inválido: {kind!r}")
    if download_only:
        convert = None  # apenas baixar: nunca há conversão
    if convert is not None:
        convert = transcode.validate(convert).to_dict()
    # readicionar o filme descarta erro anterior da MESMA variante (tmdb+idioma+
    # kind). Concluído/cancelado persistem (não são apagados na readição).
    for old_id in store.error_jobs_for(tmdb_id, language, kind):
        _jobs.pop(old_id, None)
        store.delete_job(old_id)
    # apenas baixar: o produto final fica na pasta dos torrents, então o job
    # não precisa (nem usa) um destino de arquivo final
    dest = None
    if not download_only:
        dest = store.get_destination(destination_id) if destination_id else None
        if dest is not None and dest.get("media", "movie") != "movie":
            raise ValueError(
                f"O destino '{dest['label']}' é da biblioteca de SÉRIES — "
                f"filmes vão para um destino de filmes")
        if dest is None:
            dest = store.default_destination("movie")
        if dest is None:
            raise ValueError("Nenhum destino de FILMES cadastrado — cadastre "
                             "uma pasta de destino antes")

    # destino dos torrents e opcional: sem ele, usa pasta padrao do qBittorrent
    # e nao traduz o content_path (comportamento antigo do .env)
    target = store.get_torrent_target(torrent_target_id) if torrent_target_id else None
    if target is None:
        target = store.default_torrent_target()

    job = {
        "id": uuid.uuid4().hex[:10],
        "tmdb_id": tmdb_id,
        "language": language,
        "mode": mode,
        "kind": kind,
        "download_only": download_only,
        "convert": convert,
        "status": "searching",
        "detail": "Buscando informações do filme...",
        "movie": None,
        "video_torrent": None,
        "audio_torrent": None,
        "progress": {"video": None, "audio": None},
        "output": None,
        "destination_id": dest["id"] if dest else None,
        "destination_label": dest["label"] if dest else None,
        "destination_path": dest["path"] if dest else None,
        "torrent_target_id": target["id"] if target else None,
        "torrent_target_label": target["label"] if target else None,
        "torrent_save_path": (target["save_path"] if target else "") or "",
        "torrent_local_path": (target["local_path"] if target else "") or "",
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "search": None,     # candidatos viaveis {audio: [...], video: [...]}
        "fallbacks": None,  # reservas do mesmo corte para o watchdog
        "current": None,    # candidato ativo por kind (com magnet/link p/ reinserir)
    }
    _jobs[job["id"]] = job
    tinfo = f" — torrents: {target['label']}" if target else ""
    kind_label = {"both": "original + dublado (merge)",
                  "original": "só original", "dubbed": "só dublado"}[kind]
    if download_only:
        kind_label = kind_label.replace(" (merge)", "") + ", apenas baixar"
    conv = transcode.describe(convert)
    cinfo = f" — conversão: {', '.join(conv)}" if conv else ""
    dinfo = f" — destino: {dest['label']}" if dest else ""
    _event(job, "status", f"Job criado ({kind_label}){dinfo}{tinfo}{cinfo}")
    _spawn(job["id"], _run(job))
    return _public(job)


def _probe_manual_file(path: Path, role: str) -> None:
    """Valida via ffprobe um arquivo de origem da conversão manual.

    O de vídeo precisa de stream de vídeo E de áudio (o alinhamento compara os
    dois áudios); o de áudio precisa só de áudio (pode ser um .mka, ou um vídeo
    dublado inteiro — o merger escolhe o melhor vídeo entre os dois).
    """
    try:
        probe = merger.ffprobe_json(str(path))
    except merger.MergeError:
        raise ValueError(f"'{path.name}' não parece um arquivo de vídeo/áudio válido")
    streams = probe.get("streams", [])
    # capa embutida (attached_pic) não conta como vídeo de verdade
    has_video = any(s.get("codec_type") == "video"
                    and (s.get("disposition") or {}).get("attached_pic") != 1
                    for s in streams)
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    if role == "video" and not has_video:
        raise ValueError(f"'{path.name}' não tem stream de vídeo")
    if not has_audio:
        raise ValueError(f"'{path.name}' não tem stream de áudio "
                         f"(necessário para medir o offset)")


async def create_manual(tmdb_id: int, language: str, video_path: str, audio_path: str,
                        destination_id: int | None = None,
                        convert: dict | None = None) -> dict:
    """Conversão manual: merge de dois arquivos JÁ NO DISCO, sem busca/torrents.

    Mesmo pipeline de conversão dos jobs normais (alinhamento em duas janelas,
    pausa de drift, fila de merge, saída em destino/Filme (Ano)/), só que os
    arquivos vêm de caminhos digitados pelo usuário em vez do qBittorrent.
    """
    if convert is not None:
        convert = transcode.validate(convert).to_dict()
    vf, af = Path(video_path.strip()), Path(audio_path.strip())
    for label, p in (("vídeo", vf), ("áudio", af)):
        if not str(p).strip() or not p.is_file():
            raise ValueError(f"Arquivo de {label} não existe: {p}")
    if vf.resolve() == af.resolve():
        raise ValueError("Os dois caminhos apontam para o mesmo arquivo")
    # ffprobe nos dois em paralelo: rejeita na hora o que não é mídia
    await asyncio.gather(asyncio.to_thread(_probe_manual_file, vf, "video"),
                         asyncio.to_thread(_probe_manual_file, af, "audio"))

    # readicionar o filme descarta erro anterior da mesma variante (como no create)
    for old_id in store.error_jobs_for(tmdb_id, language, "both"):
        _jobs.pop(old_id, None)
        store.delete_job(old_id)
    dest = store.get_destination(destination_id) if destination_id else None
    if dest is not None and dest.get("media", "movie") != "movie":
        raise ValueError(f"O destino '{dest['label']}' é da biblioteca de "
                         f"SÉRIES — filmes vão para um destino de filmes")
    if dest is None:
        dest = store.default_destination("movie")
    if dest is None:
        raise ValueError("Nenhum destino de FILMES cadastrado — cadastre "
                         "uma pasta de destino antes")

    job = {
        "id": uuid.uuid4().hex[:10],
        "tmdb_id": tmdb_id,
        "language": language,
        "mode": "files",  # distingue da busca auto/manual nas listas da UI
        "kind": "both",
        "convert": convert,
        "status": "merging",
        "detail": "Preparando conversão manual...",
        "movie": None,
        "video_torrent": None,
        "audio_torrent": None,
        "progress": {"video": None, "audio": None},
        "output": None,
        "destination_id": dest["id"],
        "destination_label": dest["label"],
        "destination_path": dest["path"],
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "manual_files": {"video": str(vf), "audio": str(af)},
        "search": None,
        "fallbacks": None,
        "current": None,
    }
    _jobs[job["id"]] = job
    conv = transcode.describe(convert)
    cinfo = f" — conversão: {', '.join(conv)}" if conv else ""
    _event(job, "status", f"Conversão manual criada — destino: {dest['label']}{cinfo}")
    _spawn(job["id"], _run_manual(job, vf, af))
    return _public(job)


async def _run_manual(job: dict, video_file: Path, audio_file: Path):
    """Pipeline da conversão manual: TMDB (só metadados do nome) -> fila -> merge."""
    try:
        movie = await tmdb.details(job["tmdb_id"], job["language"])
        job["movie"] = movie
        _event(job, "info", f"Filme: {movie['original_title']} ({movie['year']})")
        lock = _get_merge_lock()
        if lock.locked():
            _set(job, "merging", "Na fila de conversão...")
        async with lock:
            await delivery._merge(job, video_file, audio_file)
    except asyncio.CancelledError:
        raise
    except merger.VersionMismatch as e:
        await advanced._pause_for_drift(job, {"video": video_file, "audio": audio_file}, e)
    except Exception as e:  # noqa: BLE001
        _fail(job, f"{type(e).__name__}: {e}")


# -------------------- pipeline --------------------

async def _run(job: dict):
    try:
        await search._search(job)
        if job["mode"] == "manual":
            _set(job, "awaiting", "Escolha os torrents para baixar")
            return
        a, v = search._auto_pick(job)
        await downloads._start_download(job, a, v)
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001 - job nunca deve derrubar o servidor
        _fail(job, f"{type(e).__name__}: {e}")
        return
    await _run_from_download(job)


async def _download_and_merge(job: dict, a: dict | None, v: dict | None):
    try:
        await downloads._start_download(job, a, v)
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001
        _fail(job, f"{type(e).__name__}: {e}")
        return
    await _run_from_download(job)


async def _run_from_download(job: dict):
    try:
        paths = await downloads._wait_downloads(job)
        if job.get("download_only"):
            # apenas baixar: o download É o produto final. Nada de conversão,
            # hardlink ou cópia — e os torrents ficam no qBittorrent (seedando),
            # já que os dados baixados são exatamente o que o usuário quer.
            job["output"] = " | ".join(paths[k] for k in ("video", "audio") if k in paths)
            _set(job, "done", f"Baixado: {job['output']}")
            return
        # localiza os arquivos ANTES de entrar na fila de conversão (com retry:
        # o qBittorrent pode segurar o arquivo recém-concluído por um tempo)
        files = {kind: await downloads._resolve_video_file(job, content, kind)
                 for kind, content in paths.items()}
        # raiz do torrent por papel: as legendas externas (.srt/.ass) ficam
        # ao lado do vídeo ou em Subs/ — procuradas na hora da entrega
        job["src_roots"] = {kind: content for kind, content in paths.items()}
        # merge (ffmpeg) e entrega single (hardlink/cópia) entram na mesma fila:
        # só 1 por vez, para uma cópia grande não concorrer com uma conversão.
        lock = _get_merge_lock()
        if lock.locked():
            _set(job, "merging", "Na fila de conversão...")
        async with lock:
            if len(_needed_torrents(job)) == 1:
                await delivery._deliver_single(job, files)
            else:
                await delivery._merge(job, files["video"], files["audio"])
    except asyncio.CancelledError:
        raise
    except merger.VersionMismatch as e:
        await advanced._pause_for_drift(job, files, e)  # já fora do lock: a fila fica livre
    except Exception as e:  # noqa: BLE001
        _fail(job, f"{type(e).__name__}: {e}")
