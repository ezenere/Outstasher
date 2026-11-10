"""Jobs programados: o laço que dispara a busca na hora e as ações da UI.

Status "scheduled" = o job não está fazendo nada agora e tem hora marcada para
buscar (`next_search_at`). Dois caminhos levam a ele:
- nasceu programado (data/hora escolhida, ou episódio futuro esperando o dia
  do lançamento) e ainda não buscou (`searched_at` ausente) — a data dá para
  editar;
- buscou e não achou nada que sirva, com a busca contínua ligada: a lista do
  que apareceu continua aberta para a escolha à mão / magnet próprio, e a
  próxima busca é INCREMENTAL (services/jobs/search.carry_over).

O laço só olha o relógio; quem decide se segura (ainda não é a hora) ou busca
é o pipeline de cada mídia (movies._run / series pipeline._run), o mesmo que
roda na criação — então "Buscar agora", restart e o disparo do laço passam
pelo mesmo caminho.
"""
import asyncio
from datetime import datetime

import httpx

from services import search_retry
from services.jobs.runtime import (
    _event, _has_torrents, _jobs, _persist, _public, _set, _spawn, _tasks)

SCHEDULED = "scheduled"
LOOP_SECONDS = 20


def _due(job: dict, now: datetime) -> bool:
    nxt = job.get("next_search_at")
    if not nxt:
        return True   # programado sem hora (gravação antiga): busca já
    try:
        return datetime.fromisoformat(nxt) <= now
    except ValueError:
        return True


def wait_until(job: dict, when: datetime, detail: str):
    """Para o job até `when` (a coroutine do pipeline RETORNA depois disto)."""
    job["next_search_at"] = search_retry.fmt(when)
    _set(job, SCHEDULED, detail)


def miss(job: dict, reason: str, hint: str) -> bool:
    """A busca terminou sem nada que o job possa baixar sozinho. Com a busca
    contínua, agenda a próxima e devolve True; desligada, devolve False e
    quem chamou abre a escolha (awaiting) como sempre.

    `reason` ("Nenhum torrent encontrado para o dublado") fica guardado:
    ligar a busca contínua depois, com o job parado na escolha, agenda com ele."""
    job["search_miss"] = reason
    if not search_retry.enabled_for(job):
        _set(job, "awaiting", f"{reason} — {hint}")
        return False
    when = search_retry.next_retry()
    wait_until(job, when, f"{reason} — nova busca {search_retry.human(search_retry.fmt(when))}"
                          f" (ou escolha/cole um magnet)")
    return True


# falha de rede na busca (Jackett/TMDB fora do ar, timeout): passa — não é
# motivo para matar o job, é para tentar de novo
TRANSIENT_ERRORS = (httpx.HTTPError, asyncio.TimeoutError, ConnectionError)


def can_retry_after(job: dict, e: BaseException) -> bool:
    return isinstance(e, TRANSIENT_ERRORS) and search_retry.enabled_for(job)


def retry_after_error(job: dict, e: BaseException) -> bool:
    """A busca caiu por falha passageira e nada foi para o qBittorrent: com
    a busca contínua, agenda a próxima tentativa (True). Senão, quem chamou
    falha o job como sempre."""
    if not can_retry_after(job, e):
        return False
    when = search_retry.next_retry()
    msg = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
    _event(job, "search", f"⚠️ Busca falhou ({msg})")
    wait_until(job, when, f"Busca falhou ({type(e).__name__}) — nova tentativa "
                          f"{search_retry.human(search_retry.fmt(when))}")
    return True


def leave(job: dict):
    """O usuário agiu num job programado (escolheu, buscou de novo, resolveu
    o gate): deixa de ser programado sem evento de status próprio — quem
    chamou segue o fluxo e define o próximo."""
    if job.get("status") == SCHEDULED:
        job["status"] = "awaiting"
        job.pop("next_search_at", None)
        _persist(job)


def _fire(job: dict):
    _event(job, "chosen", "Hora da busca programada" if not job.get("searched_at")
           else "Nova tentativa da busca contínua")
    job.pop("next_search_at", None)
    if job.get("media_type") == "tv":
        from services.series import pipeline as series_pipeline
        series_pipeline.start_search(job)
    else:
        from services.jobs import movies
        _set(job, "searching", "Buscando no Jackett...")
        _spawn(job["id"], movies._run(job))


def tick(now: datetime | None = None) -> int:
    """Dispara os programados vencidos. Devolve quantos."""
    now = now or search_retry.now()
    fired = 0
    for job in list(_jobs.values()):
        if job.get("status") != SCHEDULED or not _due(job, now):
            continue
        task = _tasks.get(job["id"])
        if task is not None and not task.done():
            continue   # carregando metadados / continuação de uma escolha
        _fire(job)
        fired += 1
    return fired


async def loop():
    while True:
        try:
            tick()
        except Exception:  # noqa: BLE001 — o relógio não pode morrer
            pass
        await asyncio.sleep(LOOP_SECONDS)


# -------------------- ações da UI --------------------

def _active(job_id: str) -> dict:
    job = _jobs.get(job_id)
    if not job:
        raise ValueError("Job não encontrado entre os ativos")
    return job


def search_now(job_id: str) -> dict:
    """"Buscar agora" num programado: antecipa a hora marcada (ou a próxima
    tentativa). Episódio que ainda não estreou é procurado mesmo assim, uma
    vez — se nada aparecer, o job volta a esperar o lançamento."""
    job = _active(job_id)
    if job["status"] != SCHEDULED:
        raise ValueError("O job não está programado")
    job["search_now"] = True
    job.pop("scheduled_at", None)
    _event(job, "chosen", "Usuário pediu para buscar agora")
    job["next_search_at"] = search_retry.fmt(search_retry.now())
    tick()
    return _public(job)


def reschedule(job_id: str, at: str) -> dict:
    """Muda a data/hora de um programado que ainda não começou a procurar."""
    job = _active(job_id)
    if job["status"] != SCHEDULED or job.get("searched_at"):
        raise ValueError("Só dá para mudar a data de um job programado que "
                         "ainda não começou a procurar")
    when = search_retry.parse_when(at)
    job["scheduled_at"] = search_retry.fmt(when)
    job["next_search_at"] = job["scheduled_at"]
    _event(job, "chosen", f"Programação alterada para {search_retry.human(job['scheduled_at'])}")
    _set(job, SCHEDULED, f"Programado para {search_retry.human(job['scheduled_at'])}")
    return _public(job)


def set_retry_mode(job_id: str, mode) -> dict:
    """Liga/desliga (ou volta a herdar) a busca contínua de um job. Desligar
    um que espera a próxima tentativa abre a escolha; ligar um que está na
    escolha por falta de resultado agenda a próxima tentativa."""
    job = _active(job_id)
    mode = search_retry.validate_mode(mode)
    if mode is None:
        job.pop("retry_mode", None)
    else:
        job["retry_mode"] = mode
    how = {None: "herda a global", "on": "ligada", "off": "desligada"}[mode]
    _event(job, "chosen", f"Busca contínua deste job: {how}")
    on = search_retry.enabled_for(job)
    reason = job.get("search_miss")
    if job["status"] == SCHEDULED and job.get("searched_at") and not on:
        job.pop("next_search_at", None)
        _set(job, "awaiting", f"{reason or 'Busca sem resultado'} — escolha/cole "
                              f"um magnet ou busque de novo")
    elif (job["status"] == "awaiting" and on and reason and _has_torrents(job)
          and not job.get("drift_confirm")
          and (job.get("media_type") != "tv"
               or (job.get("awaiting") or {}).get("reason") == "manual_pick")):
        miss(job, reason, "")
    else:
        _persist(job)
    return _public(job)
