"""Orquestrador dos jobs: TMDB -> Jackett -> qBittorrent -> merge interno.

Cada job guarda um log de eventos estruturado ({ts, kind, message, data?})
persistido no banco — o frontend consome isso pela lupa (detalhe do job).

Modos:
- auto: escolhe os torrents sozinho (áudio define o corte; vídeo tem que casar).
- manual: para em "awaiting" com os candidatos viáveis; o usuário escolhe pela
  UI e o job continua via select().

O pacote é dividido em camadas, de baixo para cima:

    runtime     estado em memória, locks, eventos, hooks de ffmpeg
    views       leituras da UI (detalhe, listagens, resumo, progresso)
    search      busca no Jackett e escolha dos candidatos
    downloads   qBittorrent: envio, watchdog, troca por stall
    delivery    merge / entrega / legendas externas / limpeza dos torrents
    advanced    alinhamento avançado (EDL) quando o offset diverge
    recompress  recompressão de um filme que já está na coleção
    movies      pipeline de ponta a ponta dos jobs de filme
    actions     ações da UI (select/switch/cancel/retry) e resume

Este módulo é só a FACHADA: reexporta o que `main.py`, `services/series/*` e os
testes usam. O nome reexportado é uma CÓPIA da referência — para trocar uma
função em teste (monkeypatch), aponte para o MÓDULO DONO
(`jobs.delivery._merge`, `jobs.runtime._qbit`...), senão quem chama por dentro
do pacote continua enxergando a original.
"""
from services.jobs import (
    actions, advanced, delivery, downloads, movies, recompress, runtime,
    search, views,
)
# ---- estado e infraestrutura ----
from services.jobs.runtime import (
    KINDS, MAX_SELECTABLE, VIDEO_EXTENSIONS, _CONN_ERRORS, _cancelling,
    _delete_output, _event, _fail, _ffmpeg_hooks, _ffmpeg_procs,
    _get_merge_lock, _jobs, _lookup, _map_qbit_path, _needed_torrents, _public,
    _qbit, _register_proc, _set, _spawn, _tasks, load, poll_interval,
    progress_demanded, touch_progress_demand,
)
# ---- leituras da UI ----
from services.jobs.views import (
    _download_pct, _slim_job, counts, get_job, list_group, list_jobs, progress,
    summary,
)
# ---- pipeline e ações ----
from services.jobs.search import _dedup_results, _search
from services.jobs.downloads import _stall_limit_minutes
from services.jobs.delivery import _deliver_single, _merge
from services.jobs.advanced import _shape_verdict, proceed, resolve_review
from services.jobs.recompress import create_recompress
from services.jobs.movies import (
    _probe_manual_file, _run_from_download, create, create_manual,
)
from services.jobs.actions import (
    cancel, remove, resume_pending, retry, select, switch,
)

__all__ = [
    # submódulos (alvo de monkeypatch e uso direto)
    "actions", "advanced", "delivery", "downloads", "movies", "recompress",
    "runtime", "search", "views",
    # API usada por main.py
    "KINDS", "VIDEO_EXTENSIONS", "cancel", "counts", "create", "create_manual",
    "create_recompress", "get_job", "list_group", "list_jobs", "load",
    "poll_interval", "proceed", "progress", "remove", "resolve_review",
    "resume_pending", "retry", "select", "summary", "switch",
    "touch_progress_demand",
    # infraestrutura compartilhada com services/series/*
    "MAX_SELECTABLE", "_CONN_ERRORS", "_cancelling", "_dedup_results",
    "_delete_output", "_deliver_single", "_download_pct", "_event", "_fail",
    "_ffmpeg_hooks", "_ffmpeg_procs", "_get_merge_lock", "_jobs", "_lookup",
    "_map_qbit_path", "_merge", "_needed_torrents", "_probe_manual_file",
    "_public", "_qbit", "_register_proc", "_run_from_download", "_search",
    "_set", "_shape_verdict", "_slim_job", "_spawn", "_stall_limit_minutes",
    "_tasks", "progress_demanded",
]
