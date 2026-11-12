"""Leituras: detalhe, listagens paginadas, resumo e progresso.

Cada tela da UI faz polling no seu ritmo, então cada função devolve SÓ o que
aquela tela renderiza, em vez da lista completa de jobs.
"""

from services import candidates, stall_switch, store
from services.jobs import runtime
from services.jobs.runtime import (
    _TERMINAL_STATUSES, _is_recompress, _jobs, _lookup, _needed_torrents,
    _public)


def list_jobs() -> list[dict]:
    """Lista leve (sem eventos nem candidatos) para o polling da pagina.

    Junta os ativos (memória, com progresso ao vivo) com os terminais (banco,
    histórico) e ordena por data de criação.
    """
    terminal = store.load_jobs_by_status(_TERMINAL_STATUSES,
                                         sections=store.LIST_SECTIONS)
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
    # o evento "candidates" referencia a consulta; a lista que a lupa mostra
    # é resolvida aqui, a partir dos candidatos do job
    events = candidates.resolve_events(job, store.load_events(job_id))
    return {**_public(job), "events": events, "search": candidates.search_view(job),
            "stall": stall_view(job)}


def stall_view(job: dict) -> dict | None:
    """Troca automática de torrent travado, para a tela do job: a política
    global (o "herdar" de cada torrent mostra o que herda) e o relógio do
    watchdog por torrent — papel do filme / n da série: {idle_s, limit_s
    (None = não troca), paused, no_backup}. Só de job baixando: o relógio é
    do watchdog vivo; fora dele não há o que mostrar."""
    if job.get("status") != "downloading" or job["id"] not in _jobs:
        return None
    return {"global": stall_switch.get(), "torrents": runtime.stall_clocks(job)}


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
             "scheduled": "scheduled", "done": "done", "error": "error"}
_STATE_RANK = {"converting": 0, "downloading": 1, "searching": 2,
               "awaiting": 3, "scheduled": 4, "done": 5, "error": 6}


def _media_type(job: dict) -> str:
    """Dimensão de mídia do job. Jobs anteriores à feature de séries não têm a
    chave no JSON — são todos filmes."""
    return job.get("media_type", "movie")


def _all_jobs(media: str | None = None,
              sections: bool | tuple[str, ...] = store.LIST_SECTIONS) -> list[dict]:
    """Ativos (memória) + terminais (banco), sem duplicar. `media` filtra pela
    dimensão de mídia (movie/tv); `sections` escolhe as seções em tabela que
    os terminais trazem (store.SECTIONS) — quem só mostra título/estado/% não
    precisa dos candidatos nem dos episódios, e listagem nenhuma precisa das
    EDLs (só o detalhe)."""
    mem = [j for j in _jobs.values() if media is None or _media_type(j) == media]
    return mem + store.load_jobs_by_status(_TERMINAL_STATUSES, media=media,
                                           sections=sections)


def _movie_title(job: dict) -> str:
    m = job.get("movie")
    if m and m.get("original_title"):
        return f"{m['original_title']} ({m.get('year', '')})".strip()
    return f"TMDB #{job.get('tmdb_id')}"


# estados do qBittorrent em que o torrent ainda não tem metadados: o tamanho
# que ele reporta aí (0) não vale, e o que a troca de torrent grava no lugar
# vem da busca — tamanho do Jackett, que não é confiável
_META_STATES = ("metaDL", "forcedMetaDL")


def _real_size(p) -> int | None:
    """Tamanho REAL de um torrent (bytes dos arquivos selecionados, lido do
    qBittorrent) a partir do seu progresso; None enquanto não há metadados."""
    if not isinstance(p, dict):
        return None
    size = p.get("size") or 0
    if size <= 0 or p.get("state") in _META_STATES:
        return None
    return int(size)


def _weighted_pct(items: list[tuple[float | None, int | None, int]]) -> float:
    """Progresso agregado de vários torrents, ponderado pelo TAMANHO real.

    items: (pct 0-100 ou None, tamanho real ou None, unidades). Unidade é o
    que cada torrent entrega: 1 no filme, os episódios cobertos na série.
    Torrent sem metadados (tamanho desconhecido) vale a MÉDIA por unidade dos
    conhecidos — o que equivale a reservar para ele a fração das unidades
    (no filme, 50% enquanto o outro já tem tamanho). Sem nenhum tamanho
    conhecido, o peso é só a unidade (cada torrent 1/N do total)."""
    conhecidos = [(s, u) for _, s, u in items if s]
    if not conhecidos:
        total_u = sum(u for _, _, u in items)
        return sum((p or 0.0) * u for p, _, u in items) / total_u
    por_unidade = sum(s for s, _ in conhecidos) / sum(u for _, u in conhecidos)
    total = feito = 0.0
    for p, s, u in items:
        peso = s if s else u * por_unidade
        total += peso
        feito += (p or 0.0) / 100.0 * peso
    return 100.0 * feito / total


def _download_pct(job: dict) -> float | None:
    """Progresso do DOWNLOAD do job, 0-100, ponderado pelo tamanho real dos
    torrents (ver _weighted_pct).

    Um job dublado baixa dois torrents (vídeo original + áudio dublado) e só
    termina quando os DOIS chegam a 100%. Com 25 GB + 75 GB, o de 25 GB pronto
    é 25% do job — não 50%. Torrent ainda sem leitura do qBittorrent conta
    como 0% — o denominador é o que o job PRECISA baixar, não o que já
    reportou (senão o vídeo sozinho em 40% viraria "40% do job").

    Série: os torrents do plano, com a unidade = episódios cobertos (um pack
    de 10 episódios sem metadados pesa 10x um avulso).
    """
    if job.get("media_type") == "tv":
        torrents = job.get("torrents") or []
        pcts = [100.0 if t.get("state") == "done"
                else _pct(t.get("progress")) for t in torrents]
        if not torrents or not any(p is not None for p in pcts):
            return None
        return _weighted_pct([
            (p, _real_size(t.get("progress")), max(1, len(t.get("coverage") or [])))
            for p, t in zip(pcts, torrents)])
    needed = _needed_torrents(job)
    progs = [job["progress"].get(k) for k in needed]
    read = [_pct(p) for p in progs]
    if not any(p is not None for p in read):
        return None  # nenhum torrent reportou ainda (searching/awaiting): sem barra
    return _weighted_pct([(p, _real_size(pr), 1) for p, pr in zip(read, progs)])


def summary() -> list[dict]:
    """Lista mínima de processos EM ANDAMENTO + erros, para o dropdown do
    cabeçalho. Só o essencial para o item da lista (sem candidatos/eventos)."""
    out = []
    # o dropdown faz polling: do histórico só os ERROS (concluídos e
    # cancelados nunca aparecem nele — ler a biblioteca inteira a cada tick
    # para descartá-la era o custo da rota) e, deles, só os torrents (o % do
    # download de série sai deles)
    hist = store.load_jobs_by_status(("error",), sections=("torrents",))
    for j in list(_jobs.values()) + hist:
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
    # programados ficam fora de "Em andamento": não estão fazendo nada agora
    "scheduled": ("scheduled",),
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
    c = {"all": sum(by_status.values()), "active": 0, "scheduled": 0,
         "error": 0, "done": 0}
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
        "video_torrent": candidates.torrent_view(candidates.active(job, "video")),
        "audio_torrent": candidates.torrent_view(candidates.active(job, "audio")),
        "output": job.get("output"),
        # programação (busca na hora marcada / busca contínua)
        "next_search_at": job.get("next_search_at"),
        "searched_at": job.get("searched_at"),
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
    if group in ("active", "scheduled"):
        # os dois vivem em memória (ativos): progresso/hora marcada ao vivo
        jobs_ = [j for j in _jobs.values() if j["status"] in _GROUPS[group]
                 and (media is None or _media_type(j) == media)]
    elif group == "all":
        if per_page is not None:
            return _page_all(max(1, page), per_page, media)
        jobs_ = _all_jobs(media)
    else:
        statuses = _GROUPS.get(group)
        if not statuses:
            raise ValueError(f"grupo inválido: {group!r}")
        total = sum(store.count_jobs_by_status(media).get(s, 0) for s in statuses)
        if per_page is None:
            rows = store.load_jobs_by_status(statuses, media=media,
                                             sections=store.LIST_SECTIONS)
        else:
            page = max(1, page)
            rows = store.load_jobs_by_status(statuses, per_page,
                                             (page - 1) * per_page, media=media,
                                             sections=store.LIST_SECTIONS)
        return _page(rows, page, per_page, total)

    if group == "scheduled":
        # o próximo a buscar primeiro
        jobs_.sort(key=lambda j: j.get("next_search_at") or "")
    else:
        jobs_.sort(key=lambda j: j["created_at"], reverse=True)
    return _page(jobs_, page, per_page, len(jobs_), slice_it=True)


def _page_all(page: int, per_page: int, media: str | None) -> dict:
    """Página do grupo 'all': os ativos (memória) + só os terminais que
    podem cair na página — os page*per_page mais recentes, paginados no SQL.
    O total soma a memória com a contagem do banco (job ativo não tem status
    terminal no banco, nem terminal fica na memória: ver runtime._set)."""
    mem = [j for j in _jobs.values() if media is None or _media_type(j) == media]
    by_status = store.count_jobs_by_status(media)
    total = len(mem) + sum(by_status.get(s, 0) for s in _TERMINAL_STATUSES)
    term = store.load_jobs_by_status(_TERMINAL_STATUSES, page * per_page, 0,
                                     media=media, sections=store.LIST_SECTIONS)
    return _page(mem + term, page, per_page, total, slice_it=True)


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


def progress(job_id: str, tl_rev: int | None = None) -> dict | None:
    """Só status + detail + progresso, para o tick de 1s do detalhe do job.
    `timeline`: a do alinhamento/render em curso (tl_rev = a versão que a
    tela já tem; igual -> só o número, sem a lista de trechos de novo)."""
    job = _lookup(job_id)
    if not job:
        return None
    return {"id": job["id"], "status": job["status"], "detail": job.get("detail", ""),
            "progress": job["progress"], "output": job.get("output"),
            "merge_started_at": job.get("merge_started_at"),
            "stall": stall_view(job),
            "timeline": runtime.timeline_view(job_id, tl_rev)}
