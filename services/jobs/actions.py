"""Ações da UI e retomada após restart.

select/switch/cancel/remove/retry despacham por tipo de job (torrent, arquivos
locais, recompressão) e por mídia (filme x série).
"""

import asyncio
from pathlib import Path

from services import catalog, store
from services.jobs import downloads, movies, recompress, runtime, search
from services.jobs.runtime import (
    _TERMINAL_STATUSES, _cancelling, _delete_output, _event, _has_torrents,
    _is_local_files, _is_recompress, _jobs, _kill_ffmpeg, _lookup,
    _needed_torrents, _public, _set, _spawn, _tag, _tasks)


def resume_pending():
    """Retoma jobs interrompidos por um restart do servidor."""
    # cópia: _set(..., "error") remove o job de _jobs no meio da iteração
    for job in list(_jobs.values()):
        if job.get("media_type") == "tv":
            # pipeline de séries é desacoplado; import tardio evita ciclo
            from services.series import pipeline as series_pipeline
            series_pipeline.resume(job)
        elif _is_recompress(job):
            _resume_recompress(job)
        elif _is_local_files(job):
            _resume_manual(job)
        elif job["status"] in ("downloading", "merging"):
            job["status"] = "downloading"
            _spawn(job["id"], movies._run_from_download(job))
        elif job["status"] == "searching":
            _set(job, "error", "Servidor reiniciado durante a busca — use ↻ para repetir")
        # awaiting: candidatos estao persistidos; segue esperando a escolha


def _resume_recompress(job: dict):
    """Retoma uma recompressão interrompida por restart: recomeça do zero (o
    filme de origem está no disco). O .tmp parcial do run anterior, se sobrou,
    é ignorado — cada run usa um nome próprio com o id do job."""
    if job["status"] == "awaiting":
        return
    try:
        src = catalog.media_path(job.get("destination_id"),
                                 job["recompress"]["folder"], job["recompress"]["rel"])
    except catalog.CatalogError:
        _set(job, "error", "Arquivo da coleção não existe mais — recompressão cancelada")
        return
    _spawn(job["id"], recompress._run_recompress(job, src))


def _resume_manual(job: dict):
    """Retoma uma conversão manual interrompida por restart: os arquivos de
    origem estão no disco (não dependem do qBittorrent), então é só recomeçar
    o merge do zero. A pausa de drift (awaiting) segue esperando a decisão."""
    if job["status"] == "awaiting":
        return
    info = job["manual_files"]
    vf, af = Path(info["video"]), Path(info["audio"])
    if vf.is_file() and af.is_file():
        _spawn(job["id"], movies._run_manual(job, vf, af))
    else:
        _set(job, "error", "Arquivos de origem não existem mais — crie a conversão de novo")


# -------------------- acoes da UI --------------------

async def select(job_id: str, audio_id: str | None, video_id: str | None) -> dict | None:
    """Continuacao do modo manual: usuario escolheu o(s) torrent(s)."""
    job = _jobs.get(job_id)
    if not job or job["status"] != "awaiting":
        return None
    search = job.get("search") or {}
    needed = _needed_torrents(job)
    a = v = None
    if "audio" in needed:
        a = next((c for c in search.get("audio", []) if c["id"] == audio_id), None)
        if not a:
            raise ValueError("Candidato de áudio não encontrado (a busca pode ter sido refeita)")
    if "video" in needed:
        v = next((c for c in search.get("video", []) if c["id"] == video_id), None)
        if not v:
            raise ValueError("Candidato de vídeo não encontrado (a busca pode ter sido refeita)")
    # awaiting também acontece na pausa de drift (possível versão diferente);
    # se o usuário preferiu outro torrent em vez de Continuar, a pausa caduca
    job.pop("drift_confirm", None)
    job.pop("advanced", None)
    job.pop("awaiting", None)
    _event(job, "chosen", "Seleção manual do usuário")
    _spawn(job["id"], movies._download_and_merge(job, a, v))
    return _public(job)


async def switch(job_id: str, kind: str, candidate_id: str | None = None,
                 custom: dict | None = None) -> dict | None:
    """Troca manual de torrent durante o download.

    Sem candidate_id: "Tentar próximo" — pega o primeiro candidato reserva.
    Com candidate_id: troca para o candidato escolhido na lista da busca.
    Com custom {url, title}: magnet/link do próprio usuário (nem passa pelo
    Jackett) — o candidato entra na busca do job, então aparece na lista e
    pode voltar como reserva depois.
    """
    job = _jobs.get(job_id)
    if not job:
        return None
    if job["status"] != "downloading":
        raise ValueError("O job não está baixando — só dá para trocar torrent durante o download")
    if kind not in _needed_torrents(job):
        raise ValueError(f"Este job não baixa {kind}")
    if custom:
        nxt = search.custom_candidate(custom.get("url", ""), custom.get("title", ""))
        job.setdefault("search", {}).setdefault(kind, []).insert(0, nxt)
        _event(job, "chosen",
               f"Torrent manual informado pelo usuário ({kind}): {nxt['title']}")
    elif candidate_id:
        cands = (job.get("search") or {}).get(kind) or []
        nxt = next((c for c in cands if c["id"] == candidate_id), None)
        if not nxt:
            raise ValueError("Candidato não encontrado (a busca pode ter sido refeita)")
    else:
        fb = (job.get("fallbacks") or {}).get(kind) or []
        if not fb:
            raise ValueError(f"Sem candidato reserva de {kind} para tentar")
        nxt = fb[0]
    cur = (job.get("current") or {}).get(kind)
    if cur and cur.get("id") == nxt["id"]:
        raise ValueError("Este já é o torrent atual")
    torrents = await runtime._qbit.info_by_tag(_tag(job, kind))
    current = torrents[0] if torrents else None
    await downloads._replace_torrent(job, kind, current, nxt, "🔁 Troca manual pelo usuário")
    return _public(job)


async def cancel(job_id: str, delete_torrents: bool = False) -> dict | None:
    # ativo: memória; terminal (histórico): banco. Cancelar/limpar só faz sentido
    # no ativo — no terminal só devolvemos o job (para o remove seguir).
    job = _lookup(job_id)
    if not job:
        return None
    is_active = job_id in _jobs
    # conversão em andamento? o arquivo final está sendo escrito e precisa ser
    # apagado. Captura ANTES do cancel (o _set(cancelled) pode mexer no job).
    was_merging = is_active and job["status"] == "merging"
    if is_active:
        # marca antes de matar o ffmpeg: o merge vai levantar MergeError e o
        # pipeline não pode transformar isso em status=error (é cancelamento)
        _cancelling.add(job_id)
        # mata o ffmpeg primeiro: ele roda numa thread (to_thread), então cancelar
        # a task não o interrompe — sem isso o subprocess seguiria escrevendo
        _kill_ffmpeg(job_id)
        task = _tasks.pop(job_id, None)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    if was_merging:
        _delete_output(job)
    _cancelling.discard(job_id)
    if delete_torrents and is_active and _has_torrents(job):
        # limpeza no qBittorrent com teto de tempo: se ele estiver fora do ar,
        # a remoção do job NÃO pode ficar travada esperando o timeout de rede.
        # Série: todos os torrents do job carregam a tag compartilhada dl-{id};
        # filme: uma tag por papel (video/audio).
        tags = ([f"dl-{job['id']}"] if job.get("media_type") == "tv"
                else [_tag(job, k) for k in ("video", "audio")])
        for tag in tags:
            try:
                await asyncio.wait_for(
                    runtime._qbit.delete_by_tag(tag, delete_files=True), timeout=10)
            except (Exception, asyncio.TimeoutError) as e:  # noqa: BLE001
                reason = "sem resposta (qBittorrent fora do ar?)" if isinstance(
                    e, asyncio.TimeoutError) else str(e)
                _event(job, "qbit",
                       f"⚠️ Não removi torrent(s) da tag {tag}: {reason} — "
                       f"apague manualmente no qBittorrent se precisar")
    if job.get("media_type") == "tv":
        from services.series import pipeline as series_pipeline
        series_pipeline.forget(job_id)
    if job["status"] not in _TERMINAL_STATUSES:
        _set(job, "cancelled",
             "Cancelado pelo usuário" + (" (torrents removidos)" if delete_torrents else ""))
    return job


async def remove(job_id: str, delete_torrents: bool = False) -> bool:
    job = await cancel(job_id, delete_torrents)
    if not job:
        return False
    _jobs.pop(job_id, None)
    store.delete_job(job_id)
    return True


async def retry(job_id: str) -> dict | None:
    old = _lookup(job_id)  # jobs em erro/cancelados vivem só no banco agora
    if not old or old["status"] not in ("error", "cancelled"):
        return None
    if old.get("media_type") == "tv":
        from services.series import pipeline as series_pipeline
        return await series_pipeline.retry(old)
    if _is_recompress(old):
        r = old["recompress"]
        return await recompress.create_recompress(old.get("destination_id"), r["folder"], r["rel"],
                                       old["convert"], r.get("replace", True),
                                       old.get("tmdb_id"))
    if _is_local_files(old):
        return await movies.create_manual(old["tmdb_id"], old["language"],
                                   old["manual_files"]["video"],
                                   old["manual_files"]["audio"],
                                   old.get("destination_id"), old.get("convert"))
    return await movies.create(old["tmdb_id"], old["language"], old.get("mode", "auto"),
                        old.get("destination_id"), old.get("torrent_target_id"),
                        old.get("kind", "both"), old.get("download_only", False),
                        old.get("convert"))
