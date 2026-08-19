"""Leituras: detalhe, listagens paginadas, resumo e progresso.

Cada tela da UI faz polling no seu ritmo, então cada função devolve SÓ o que
aquela tela renderiza, em vez da lista completa de jobs.
"""

from services import store
from services.jobs import runtime
from services.jobs.runtime import (
    _TERMINAL_STATUSES, _is_recompress, _jobs, _lookup, _needed_torrents,
    _public)


def list_jobs() -> list[dict]:
    """Lista leve (sem eventos nem candidatos) para o polling da pagina.

    Junta os ativos (memória, com progresso ao vivo) com os terminais (banco,
    histórico) e ordena por data de criação.
    """
    terminal = store.load_jobs_by_status(_TERMINAL_STATUSES)
    combined = list(_jobs.values()) + terminal
    ordered = sorted(combined, key=lambda j: j["created_at"], reverse=True)
    return [_public(j) for j in ordered]


def get_job(job_id: str) -> dict | None:
    """Job completo: eventos (para a lupa) + candidatos (para a escolha manual).

    Ativo vem da memória (progresso ao vivo); terminal vem do banco.
    """
    job = _lookup(job_id)
    if not job:
        return None
    return {**_public(job), "events": store.load_events(job_id), "search": job.get("search")}


# -------------------- leituras enxutas (polling granular) --------------------
# O frontend faz polling em ritmos diferentes por tela; cada rota traz SÓ o
# mínimo que aquela tela renderiza, em vez da lista completa de jobs.

def _pct(p) -> float | None:
    """Extrai o percentual (0-100) de um valor de progresso (objeto ou número)."""
    if p is None:
        return None
    if isinstance(p, (int, float)):
        return float(p)
    if isinstance(p, dict):
        return p.get("pct")
    return None


def _slim_progress(p) -> dict | None:
    """Progresso de um torrent para o card da lista: % + baixado/total.

    Velocidade, ETA e seeds continuam só no detalhe do job. Jobs antigos (do
    banco) podem ter só o número do percentual — daí o _pct no meio."""
    pct = _pct(p)
    if pct is None:
        return None
    d = p if isinstance(p, dict) else {}
    return {"pct": pct, "downloaded": d.get("downloaded"), "size": d.get("size"),
            "state": d.get("state")}


# status -> estado visual do filme/processo (menor rank = maior prioridade).
# cancelled não vira estado (some da UI). done/error contam como histórico.
_STATE_OF = {"merging": "converting", "downloading": "downloading",
             "searching": "searching", "awaiting": "awaiting",
             "done": "done", "error": "error"}
_STATE_RANK = {"converting": 0, "downloading": 1, "searching": 2,
               "awaiting": 3, "done": 4, "error": 5}


def _media_type(job: dict) -> str:
    """Dimensão de mídia do job. Jobs anteriores à feature de séries não têm a
    chave no JSON — são todos filmes."""
    return job.get("media_type", "movie")


def _all_jobs(media: str | None = None) -> list[dict]:
    """Ativos (memória) + terminais (banco), sem duplicar. `media` filtra pela
    dimensão de mídia (movie/tv)."""
    mem = [j for j in _jobs.values() if media is None or _media_type(j) == media]
    return mem + store.load_jobs_by_status(_TERMINAL_STATUSES, media=media)


def _movie_title(job: dict) -> str:
    m = job.get("movie")
    if m and m.get("original_title"):
        return f"{m['original_title']} ({m.get('year', '')})".strip()
    return f"TMDB #{job.get('tmdb_id')}"


def _download_pct(job: dict) -> float | None:
    """Progresso do DOWNLOAD do job, 0-100, como média dos torrents que ele baixa.

    Um job dublado baixa dois torrents (vídeo original + áudio dublado) e só
    termina quando os DOIS chegam a 100%, então cada um vale 50%. Torrent ainda
    sem leitura do qBittorrent conta como 0% — o denominador é o que o job
    PRECISA baixar, não o que já reportou (senão o vídeo sozinho em 40% viraria
    "40% do job", e a barra andaria para trás quando o áudio aparecesse).

    Série: média dos torrents do plano PONDERADA pela cobertura (um pack de 10
    episódios pesa 10x um avulso — a barra reflete episódios, não torrents).
    """
    if job.get("media_type") == "tv":
        torrents = job.get("torrents") or []
        weights = [max(1, len(t.get("coverage") or [])) for t in torrents]
        pcts = [100.0 if t.get("state") == "done"
                else _pct(t.get("progress")) for t in torrents]
        if not torrents or not any(p is not None for p in pcts):
            return None
        total = sum(weights)
        return sum((p or 0.0) * w for p, w in zip(pcts, weights)) / total
    needed = _needed_torrents(job)
    read = [_pct(job["progress"].get(k)) for k in needed]
    if not any(p is not None for p in read):
        return None  # nenhum torrent reportou ainda (searching/awaiting): sem barra
    return sum(p or 0.0 for p in read) / len(read)


def summary() -> list[dict]:
    """Lista mínima de processos EM ANDAMENTO + erros, para o dropdown do
    cabeçalho. Só o essencial para o item da lista (sem candidatos/eventos)."""
    out = []
    for j in _all_jobs():
        state = _STATE_OF.get(j["status"])
        if state in (None, "done"):  # dropdown ignora concluídos e cancelados
            continue
        pct = ((j["progress"].get("merge") or {}).get("pct") if state == "converting"
               else _download_pct(j))
        # recompress: reusa o tmdb_id de um filme já baixado, então quem chaveia
        # por tmdb_id (tela de Filmes) ignora; o dropdown mostra
        out.append({"id": j["id"], "tmdb_id": j.get("tmdb_id"),
                    "title": _movie_title(j), "status": j["status"],
                    "state": state, "pct": pct, "phase": _merge_phase(j),
                    "media_type": _media_type(j),
                    "recompress": _is_recompress(j)})
    out.sort(key=lambda x: _STATE_RANK[x["state"]])
    return out


# grupos de status expostos no filtro da tela de Downloads
_GROUPS = {
    "active": ("searching", "awaiting", "downloading", "merging"),
    "error": ("error", "cancelled"),
    "done": ("done",),
}


def counts(media: str | None = None) -> dict[str, int]:
    """Contagem por grupo (active/error/done/all) para os badges do filtro.

    O banco é a fonte: todo job (ativo ou terminal) tem uma linha lá e as
    transições de status persistem na hora (via _event), então o `status` no
    banco está sempre atualizado — não precisa somar a memória por cima.
    `media` ("movie"/"tv") restringe à dimensão de mídia.
    """
    by_status = store.count_jobs_by_status(media)
    c = {"all": sum(by_status.values()), "active": 0, "error": 0, "done": 0}
    for group, statuses in _GROUPS.items():
        c[group] = sum(by_status.get(s, 0) for s in statuses)
    return c


def _merge_phase(job: dict) -> str | None:
    """Em que etapa da conversão o job está (None quando não está convertendo).

    Sai do progresso do ffmpeg, que é quem sabe — assim o dropdown, a lista e
    o detalhe dizem todos a mesma coisa."""
    if job["status"] != "merging":
        return None
    return (job["progress"].get("merge") or {}).get("phase") or runtime.PHASE_CONVERT


def _slim_job(job: dict) -> dict:
    """Job enxuto para os cards da lista de Downloads: sem search/eventos/
    candidatos. Nos torrents vai % + baixado/total; velocidade, ETA e seeds
    ficam no detalhe do job."""
    return {
        "id": job["id"], "tmdb_id": job.get("tmdb_id"), "language": job["language"],
        "media_type": _media_type(job),
        "mode": job.get("mode"), "kind": job.get("kind", "both"),
        "download_only": job.get("download_only", False),
        "convert": bool(job.get("convert")),
        "status": job["status"], "detail": job.get("detail", ""),
        "movie": job.get("movie"), "created_at": job["created_at"],
        "destination_label": job.get("destination_label"),
        "video_torrent": job.get("video_torrent"),
        "audio_torrent": job.get("audio_torrent"),
        "output": job.get("output"),
        "progress": {
            "video": _slim_progress(job["progress"].get("video")),
            "audio": _slim_progress(job["progress"].get("audio")),
            "merge": (job["progress"].get("merge") or {}).get("pct")
            if job["progress"].get("merge") else None,
            "merge_phase": _merge_phase(job),
            # leitura (frames lidos pelo encoder) para a barra sobreposta do card
            "merge_read": (job["progress"].get("merge") or {}).get("read_pct")
            if job["progress"].get("merge") else None,
        },
        **_slim_series(job),
    }


def _slim_series(job: dict) -> dict:
    """Resumo de série para o card da lista (nada por episódio — isso é do
    detalhe): contagens por estado + % agregado + gate ativo."""
    if job.get("media_type") != "tv":
        return {}
    eps = job.get("episodes") or {}
    by_state: dict[str, int] = {}
    for v in eps.values():
        by_state[v["state"]] = by_state.get(v["state"], 0) + 1
    return {"series": {
        "episodes_total": len(eps),
        "by_state": by_state,
        "download_pct": _download_pct(job),
        "awaiting_reason": (job.get("awaiting") or {}).get("reason"),
    }}


def list_group(group: str = "active", page: int = 1,
               per_page: int | None = None, media: str | None = None) -> dict:
    """Cards enxutos da tela de Jobs, filtrados por grupo NO BACKEND.

    Devolve {"items": [...], "page", "per_page", "total", "pages"}. Sem
    `per_page` volta tudo numa página só (o total continua correto).

    Os grupos só de status terminal paginam no SQL; 'active' e 'all' envolvem a
    memória (os ativos não estão só no banco) e paginam depois de montar a
    lista — 'active' é curto por natureza, então o custo é irrelevante.
    `media` ("movie"/"tv") filtra pela dimensão de mídia, combinável com o grupo.
    """
    if group == "active":
        jobs_ = [j for j in _jobs.values() if j["status"] in _GROUPS["active"]
                 and (media is None or _media_type(j) == media)]
    elif group == "all":
        jobs_ = _all_jobs(media)
    else:
        statuses = _GROUPS.get(group)
        if not statuses:
            raise ValueError(f"grupo inválido: {group!r}")
        total = sum(store.count_jobs_by_status(media).get(s, 0) for s in statuses)
        if per_page is None:
            rows = store.load_jobs_by_status(statuses, media=media)
        else:
            page = max(1, page)
            rows = store.load_jobs_by_status(statuses, per_page,
                                             (page - 1) * per_page, media=media)
        return _page(rows, page, per_page, total)

    jobs_.sort(key=lambda j: j["created_at"], reverse=True)
    return _page(jobs_, page, per_page, len(jobs_), slice_it=True)


def _page(jobs_: list[dict], page: int, per_page: int | None, total: int,
          slice_it: bool = False) -> dict:
    """Empacota a resposta paginada. `slice_it` recorta em memória (grupos que
    não puderam paginar no SQL); os que já vieram paginados do banco não."""
    if per_page is None:
        return {"items": [_slim_job(j) for j in jobs_], "page": 1,
                "per_page": total, "total": total, "pages": 1}
    page = max(1, page)
    if slice_it:
        jobs_ = sorted(jobs_, key=lambda j: j["created_at"], reverse=True)
        jobs_ = jobs_[(page - 1) * per_page:page * per_page]
    return {"items": [_slim_job(j) for j in jobs_], "page": page,
            "per_page": per_page, "total": total,
            "pages": max(1, -(-total // per_page))}


def progress(job_id: str) -> dict | None:
    """Só status + detail + progresso, para o tick de 1s do detalhe do job."""
    job = _lookup(job_id)
    if not job:
        return None
    return {"id": job["id"], "status": job["status"], "detail": job.get("detail", ""),
            "progress": job["progress"], "output": job.get("output"),
            "merge_started_at": job.get("merge_started_at")}
