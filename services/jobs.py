"""Orquestrador: TMDB -> Jackett -> qBittorrent -> merge interno.

Cada job guarda um log de eventos estruturado ({ts, kind, message, data?})
persistido em jobs.json — o frontend consome isso pela lupa (detalhe do job).

Modos:
- auto: escolhe os torrents sozinho (áudio define o corte; vídeo tem que casar).
- manual: para em "awaiting" com os candidatos viáveis; o usuário escolhe
  pela UI e o job continua via select().

Watchdog: download sem progresso é trocado pelo próximo candidato viável do
mesmo corte (job["fallbacks"]). O timeout depende do estado do torrent:
metaDL espera mais (30 min) e stalledDL muito mais (2 h). Perder a conexão
com o qBittorrent nunca falha o job — ele avisa e fica tentando reconectar;
torrent que sumiu do qBittorrent é reinserido automaticamente.
"""
import asyncio
import re
import time
import uuid
from datetime import datetime
from pathlib import Path

import httpx

import config
from services import jackett, merger, selector, store, tmdb
from services.qbittorrent import QbitClient, QbitError

# problemas de comunicação com o qBittorrent que NÃO devem falhar o job
# durante o download: rede fora, sessão caída, restart do qBittorrent...
_CONN_ERRORS = (httpx.HTTPError, QbitError, OSError)

VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".m2ts", ".mov", ".wmv", ".mpg", ".mpeg"}
MAX_SELECTABLE = 30  # candidatos guardados por papel para selecao manual/fallback

# estados: searching -> (awaiting ->) downloading -> merging -> done | error | cancelled
_jobs: dict[str, dict] = {}
_tasks: dict[str, asyncio.Task] = {}
_qbit = QbitClient()

# fila de conversão: só 1 merge/entrega roda por vez (ffmpeg é pesado de CPU/IO).
# lazy porque o event loop pode não existir no import.
_merge_lock: asyncio.Lock | None = None


def _get_merge_lock() -> asyncio.Lock:
    global _merge_lock
    if _merge_lock is None:
        _merge_lock = asyncio.Lock()
    return _merge_lock


def load():
    store.init()
    for job in store.load_jobs():
        _jobs[job["id"]] = job


def resume_pending():
    """Retoma jobs interrompidos por um restart do servidor."""
    for job in _jobs.values():
        if job["status"] in ("downloading", "merging"):
            job["status"] = "downloading"
            _event(job, "status", "Servidor reiniciado — retomando acompanhamento dos downloads")
            _tasks[job["id"]] = asyncio.create_task(_run_from_download(job))
        elif job["status"] == "searching":
            _set(job, "error", "Servidor reiniciado durante a busca — use ↻ para tentar de novo")
        # awaiting: candidatos estao persistidos; segue esperando a escolha


def _public(job: dict) -> dict:
    return {k: v for k, v in job.items() if k not in ("events", "search")}


def list_jobs() -> list[dict]:
    """Lista leve (sem eventos nem candidatos) para o polling da pagina."""
    ordered = sorted(_jobs.values(), key=lambda j: j["created_at"], reverse=True)
    return [_public(j) for j in ordered]


def get_job(job_id: str) -> dict | None:
    """Job completo: eventos (para a lupa) + candidatos (para a escolha manual)."""
    job = _jobs.get(job_id)
    if not job:
        return None
    return {**_public(job), "events": store.load_events(job_id), "search": job.get("search")}


def _event(job: dict, kind: str, message: str, data=None):
    ev = {"ts": datetime.now().isoformat(timespec="seconds"), "kind": kind, "message": message}
    if data is not None:
        ev["data"] = data
    store.add_event(job["id"], ev)
    store.upsert_job(job)  # status/detail quase sempre mudam junto com o evento


def _set(job: dict, status: str, detail: str = ""):
    job["status"] = status
    job["detail"] = detail
    _event(job, "status", detail or status)


def _fail(job: dict, message: str):
    _set(job, "error", message)


# Tipos de job: o que baixar/entregar.
#   both     -> baixa vídeo original + áudio dublado e faz o merge (padrão)
#   original -> baixa só o vídeo original e entrega direto (sem merge)
#   dubbed   -> baixa só a versão dublada e entrega direto (sem merge)
KINDS = ("both", "original", "dubbed")


def _needed_torrents(job: dict) -> tuple[str, ...]:
    """Quais torrents este job baixa: ('video',), ('audio',) ou os dois."""
    kind = job.get("kind", "both")
    if kind == "original":
        return ("video",)
    if kind == "dubbed":
        return ("audio",)
    return ("video", "audio")


async def create(tmdb_id: int, language: str, mode: str = "auto",
                 destination_id: int | None = None,
                 torrent_target_id: int | None = None,
                 kind: str = "both") -> dict:
    if kind not in KINDS:
        raise ValueError(f"kind inválido: {kind!r}")
    dest = store.get_destination(destination_id) if destination_id else None
    if dest is None:
        dest = store.default_destination()
    if dest is None:
        raise ValueError("Nenhum destino cadastrado — cadastre uma pasta de destino antes")

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
        "status": "searching",
        "detail": "Buscando informações do filme...",
        "movie": None,
        "video_torrent": None,
        "audio_torrent": None,
        "progress": {"video": None, "audio": None},
        "output": None,
        "destination_id": dest["id"],
        "destination_label": dest["label"],
        "destination_path": dest["path"],
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
    _event(job, "status",
           f"Job criado ({kind_label}, modo {mode}) — destino: {dest['label']} ({dest['path']}){tinfo}")
    _tasks[job["id"]] = asyncio.create_task(_run(job))
    return _public(job)


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
    _event(job, "chosen", "Seleção manual do usuário")
    _tasks[job["id"]] = asyncio.create_task(_download_and_merge(job, a, v))
    return _public(job)


async def switch(job_id: str, kind: str, candidate_id: str | None = None) -> dict | None:
    """Troca manual de torrent durante o download.

    Sem candidate_id: "Tentar próximo" — pega o primeiro candidato reserva.
    Com candidate_id: troca para o candidato escolhido na lista da busca.
    """
    job = _jobs.get(job_id)
    if not job:
        return None
    if job["status"] != "downloading":
        raise ValueError("O job não está baixando — só dá para trocar torrent durante o download")
    if kind not in _needed_torrents(job):
        raise ValueError(f"Este job não baixa {kind}")
    if candidate_id:
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
    torrents = await _qbit.info_by_tag(_tag(job, kind))
    current = torrents[0] if torrents else None
    await _replace_torrent(job, kind, current, nxt, "🔁 Troca manual pelo usuário")
    return _public(job)


async def cancel(job_id: str, delete_torrents: bool = False) -> dict | None:
    job = _jobs.get(job_id)
    if not job:
        return None
    task = _tasks.pop(job_id, None)
    if task and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    if delete_torrents:
        for kind in ("video", "audio"):
            try:
                await _qbit.delete_by_tag(_tag(job, kind), delete_files=True)
            except Exception as e:  # noqa: BLE001
                _event(job, "qbit", f"Falha ao remover torrent de {kind}: {e}")
    if job["status"] not in ("done", "error", "cancelled"):
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
    old = _jobs.get(job_id)
    if not old or old["status"] not in ("error", "cancelled"):
        return None
    return await create(old["tmdb_id"], old["language"], old.get("mode", "auto"),
                        old.get("destination_id"), old.get("torrent_target_id"),
                        old.get("kind", "both"))


# -------------------- pipeline --------------------

async def _run(job: dict):
    try:
        await _search(job)
        if job["mode"] == "manual":
            _set(job, "awaiting",
                 "Busca concluída — clique em Escolher para selecionar os torrents")
            return
        a, v = _auto_pick(job)
        await _start_download(job, a, v)
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001 - job nunca deve derrubar o servidor
        _fail(job, f"{type(e).__name__}: {e}")
        return
    await _run_from_download(job)


async def _download_and_merge(job: dict, a: dict | None, v: dict | None):
    try:
        await _start_download(job, a, v)
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001
        _fail(job, f"{type(e).__name__}: {e}")
        return
    await _run_from_download(job)


def _slim(cand: dict, cid: str) -> dict:
    return {"id": cid, "title": cand["title"], "tracker": cand.get("tracker"),
            "seeders": cand["seeders"], "size": cand["size"],
            "edition": cand.get("edition"), "score": cand["score"],
            "magnet": cand.get("magnet"), "link": cand.get("link")}


def _extra_searches(localized: str, year: str, lang: str) -> list[dict]:
    """Buscas extras direcionadas conforme as regras (idioma x variante x indexers).

    Cada variante só entra se produzir uma query DIFERENTE da localized normal e
    se houver indexers configurados para ela no idioma. Retorna uma lista de
    {query, indexer, variant} — uma entrada por indexer.
    """
    rules = store.get_extra_search_rules().get(lang) or {}
    if not localized or not rules:
        return []

    base = f"{localized} {year}".strip()
    arabic = selector._roman_to_arabic(localized)
    has_roman = selector.has_roman_numeral(localized)

    # cada variante -> a query que ela gera (ou None se não se aplica ao título)
    variant_query = {
        "no_year": localized if year else None,
        "roman": f"{arabic} {year}".strip() if has_roman else None,
        "roman_no_year": arabic if (has_roman and year) else None,
    }

    out: list[dict] = []
    seen_queries = {base.lower()}
    for variant, query in variant_query.items():
        if not query:
            continue
        indexers = rules.get(variant) or []
        if not indexers:
            continue
        if query.lower() in seen_queries:
            continue  # não repete uma query que outra variante já cobriu
        seen_queries.add(query.lower())
        for idx in indexers:
            out.append({"query": query, "indexer": idx, "variant": variant})
    return out


async def _run_extra_search(job: dict, spec: dict) -> list[dict]:
    """Roda uma busca extra; falha de um indexer não derruba o job."""
    try:
        res = await jackett.search(spec["query"], spec["indexer"])
        _event(job, "search",
               f"Busca extra [{spec['variant']} @ {spec['indexer']}] '{spec['query']}' "
               f"→ {len(res)} resultados")
        return res
    except Exception as e:  # noqa: BLE001
        _event(job, "search",
               f"⚠️ Busca extra [{spec['variant']} @ {spec['indexer']}] '{spec['query']}' "
               f"falhou: {type(e).__name__}: {e}")
        return []


async def _search(job: dict):
    """Busca no Jackett e preenche job["search"] com os candidatos viáveis."""
    lang = job["language"]
    label = config.LANGUAGES[lang]["label"]
    movie = await tmdb.details(job["tmdb_id"], lang)
    job["movie"] = movie
    original, localized, year = movie["original_title"], movie["localized_title"], movie["year"]
    _event(job, "info", f"Filme: {original} ({year}) — título em {label}: {localized}")

    needed = _needed_torrents(job)
    want_video = "video" in needed
    want_audio = "audio" in needed

    # buscas extras direcionadas (só afetam o áudio dublado): rodam em paralelo
    extra_specs = _extra_searches(localized, year, lang) if want_audio else []

    query_original = f"{original} {year}".strip()
    has_localized = bool(localized and localized.lower() != (original or "").lower())
    query_localized = f"{localized} {year}".strip() if has_localized else None
    _set(job, "searching",
         f"Procurando '{query_original}' no Jackett (pode levar vários minutos)...")
    if extra_specs:
        _event(job, "search",
               f"{len(extra_specs)} busca(s) extra(s) configurada(s) para {label} "
               f"— rodando em paralelo")

    # dispara TODAS as buscas em paralelo (all + localized + extras direcionadas)
    tasks = [jackett.search(query_original)]
    if query_localized:
        tasks.append(jackett.search(query_localized))
    for spec in extra_specs:
        tasks.append(_run_extra_search(job, spec))
    all_results = await asyncio.gather(*tasks)

    results_original = all_results[0]
    _event(job, "search", f"Jackett devolveu {len(results_original)} resultados para '{query_original}'")
    idx = 1
    results_localized = []
    if query_localized:
        results_localized = all_results[idx]
        idx += 1
    extra_results = all_results[idx:]  # já logados dentro de _run_extra_search

    # ---- audio dublado: titulo traduzido + titulo original com marcador ----
    audio_viable = []
    if want_audio:
        _set(job, "searching", f"Avaliando versão em {label}...")
        audio_ranked = []
        # resultados do título traduzido + das buscas extras entram como tier 0
        # (título no idioma dublado tem preferência máxima)
        localized_pool = list(results_localized)
        for r in extra_results:
            localized_pool.extend(r)
        if localized_pool:
            ranked, trace = selector.rank(localized_pool, "audio", localized, year,
                                          language=lang)
            _event(job, "candidates", f"Avaliação para ÁUDIO — título em {label} (+ buscas extras)",
                   {"role": "audio", "query": query_localized or localized, "candidates": trace})
            for c in ranked:
                c["tier"] = 0  # titulo no idioma dublado: preferencia maxima
            audio_ranked.extend(ranked)

        ranked, trace = selector.rank(results_original, "audio", original, year,
                                      language=lang, require_language=True)
        _event(job, "candidates",
               f"Avaliação para ÁUDIO — busca '{query_original}' exigindo marcador de {label}",
               {"role": "audio", "query": query_original, "candidates": trace})
        for c in ranked:
            # titulo original MAS com o titulo traduzido junto (release "Título / Title")
            # ainda conta como dublado confirmado; senao e so fallback
            c["tier"] = 0 if localized and selector.matches_title(c["title"], localized) else 1
        audio_ranked.extend(ranked)

        # dedupe (o mesmo torrent pode aparecer nas duas buscas) e ordena:
        # titulo dublado (tier 0) SEMPRE antes de ingles+marcador (tier 1);
        # score decide dentro de cada tier
        seen = set()
        for c in sorted(audio_ranked, key=lambda r: (r.get("tier", 1), -r["score"])):
            key = c.get("magnet") or c.get("link")
            if key in seen:
                continue
            seen.add(key)
            audio_viable.append(c)
        n_localized = sum(1 for c in audio_viable if c.get("tier") == 0)
        if n_localized and n_localized < len(audio_viable):
            _event(job, "info",
                   f"Preferência de áudio: {n_localized} candidato(s) com título em {label} "
                   f"na frente de {len(audio_viable) - n_localized} em inglês com marcador")

    # ---- video: titulo original, qualquer corte (o filtro vem depois) ----
    video_viable = []
    if want_video:
        video_viable, trace = selector.rank(results_original, "video", original, year)
        _event(job, "candidates", f"Avaliação para VÍDEO — busca '{query_original}'",
               {"role": "video", "query": query_original, "candidates": trace})

    if want_audio and not audio_viable:
        raise RuntimeError(f"Nenhum torrent encontrado com áudio em {label}")
    if want_video and not video_viable:
        raise RuntimeError(f"Nenhum torrent de vídeo viável para '{original}'")

    job["search"] = {
        "audio": [_slim(c, f"a{i}") for i, c in enumerate(audio_viable[:MAX_SELECTABLE])],
        "video": [_slim(c, f"v{i}") for i, c in enumerate(video_viable[:MAX_SELECTABLE])],
    }
    store.upsert_job(job)


def _auto_pick(job: dict) -> tuple[dict | None, dict | None]:
    """Escolhe o(s) torrent(s) automaticamente conforme o tipo do job.

    - dubbed:   melhor áudio (sem vídeo).
    - original: melhor vídeo (sem áudio).
    - both:     melhor áudio define o corte; melhor vídeo do MESMO corte.
    """
    search = job["search"]
    needed = _needed_torrents(job)
    if needed == ("audio",):
        return search["audio"][0], None
    if needed == ("video",):
        return None, search["video"][0]

    for a in search["audio"]:
        ed_label = a["edition"] or "normal"
        vids = [v for v in search["video"] if v["edition"] == a["edition"]]
        if vids:
            _event(job, "info",
                   f"Corte definido pelo áudio: '{ed_label}' — {len(vids)} vídeos compatíveis")
            return a, vids[0]
        _event(job, "info",
               f"Nenhum vídeo com corte '{ed_label}' para casar com "
               f"'{a['title']}' — tentando o próximo candidato de áudio")
    raise RuntimeError(
        "Nenhum torrent de vídeo com o mesmo corte das versões dubladas encontradas "
        "(as duas versões precisam ser do mesmo corte para os áudios alinharem)")


async def _start_download(job: dict, a: dict | None, v: dict | None):
    if a:
        _event(job, "chosen", f"🔊 Áudio: {a['title']} (score {a['score']}, "
                              f"{a['seeders']} seeds, corte {a['edition'] or 'normal'})")
        job["audio_torrent"] = {"title": a["title"], "seeders": a["seeders"],
                                "size": a["size"], "score": a["score"], "edition": a["edition"]}
    if v:
        _event(job, "chosen", f"🎥 Vídeo: {v['title']} (score {v['score']}, "
                              f"{v['seeders']} seeds, corte {v['edition'] or 'normal'})")
        job["video_torrent"] = {"title": v["title"], "seeders": v["seeders"],
                                "size": v["size"], "score": v["score"], "edition": v["edition"]}

    # reservas do mesmo corte, para o watchdog trocar se o download travar
    search = job.get("search") or {"audio": [], "video": []}
    job["fallbacks"] = {
        "audio": [x for x in search["audio"]
                  if a and x["edition"] == a["edition"] and x["id"] != a["id"]],
        "video": [x for x in search["video"]
                  if v and x["edition"] == v["edition"] and x["id"] != v["id"]],
    }
    # candidato ativo (com magnet/link): usado para reinserir se o torrent
    # sumir do qBittorrent e para a troca manual saber o que está rodando
    job["current"] = {"video": v, "audio": a}

    _set(job, "searching", "Enviando torrents para o qBittorrent...")
    save_path = job.get("torrent_save_path") or config.QBIT_SAVE_PATH or None
    url_video = (v.get("magnet") or v["link"]) if v else None
    url_audio = (a.get("magnet") or a["link"]) if a else None

    if v and a and url_video == url_audio:
        # mesmo torrent serve para os dois (ex.: release dual audio)
        await _qbit.add(url_video, f"{_tag(job, 'video')},{_tag(job, 'audio')}", save_path)
        _event(job, "qbit", "Mesmo torrent serve para vídeo e áudio — adicionado uma única vez")
    else:
        if v:
            await _qbit.add(url_video, _tag(job, "video"), save_path)
            _event(job, "qbit", f"Torrent de vídeo adicionado ao qBittorrent (tag {_tag(job, 'video')})")
        if a:
            await _qbit.add(url_audio, _tag(job, "audio"), save_path)
            _event(job, "qbit", f"Torrent de áudio adicionado ao qBittorrent (tag {_tag(job, 'audio')})")
    if save_path:
        _event(job, "qbit", f"Salvando em: {save_path}")
    _set(job, "downloading", "Baixando torrent..." if len(_needed_torrents(job)) == 1
         else "Baixando torrents...")


def _tag(job: dict, kind: str) -> str:
    return f"dl-{job['id']}-{kind}"


async def _run_from_download(job: dict):
    try:
        paths = await _wait_downloads(job)
        # localiza os arquivos ANTES de entrar na fila de conversão (com retry:
        # o qBittorrent pode segurar o arquivo recém-concluído por um tempo)
        files = {kind: await _resolve_video_file(job, content, kind)
                 for kind, content in paths.items()}
        # merge (ffmpeg) e entrega single (hardlink/cópia) entram na mesma fila:
        # só 1 por vez, para uma cópia grande não concorrer com uma conversão.
        lock = _get_merge_lock()
        if lock.locked():
            _set(job, "merging", "Na fila de conversão — aguardando a conversão anterior terminar...")
        async with lock:
            if len(_needed_torrents(job)) == 1:
                await _deliver_single(job, files)
            else:
                await _merge(job, files["video"], files["audio"])
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001
        _fail(job, f"{type(e).__name__}: {e}")


async def _resolve_video_file(job: dict, content_path: str, kind: str) -> Path:
    """Localiza o arquivo de vídeo com retry para erros transitórios de I/O.

    No WSL, stat/listagem em /mnt/* (drvfs/9p) falha com EINVAL enquanto um
    processo Windows (o qBittorrent, logo após concluir) ainda segura o
    arquivo. Espera com backoff em vez de falhar o job.
    """
    delays = (5, 15, 30, 60, 120, 300, 600)  # ~19 min no total
    for i, delay in enumerate(delays):
        try:
            return await asyncio.to_thread(_find_video_file, job, content_path)
        except OSError as e:
            if i == 0:
                _event(job, "info",
                       f"⚠️ Arquivo de {kind} ainda inacessível ({e}) — o qBittorrent "
                       f"pode estar verificando/movendo o download; aguardando...")
            job["detail"] = (f"Aguardando o arquivo de {kind} ficar acessível "
                             f"(tentativa {i + 1}/{len(delays) + 1})...")
            await asyncio.sleep(delay)
    # última tentativa: se ainda falhar, o erro real sobe e o job falha
    return await asyncio.to_thread(_find_video_file, job, content_path)


def _stall_limit_minutes(state: str) -> int:
    """Timeout de stall conforme o estado do torrent.

    metaDL (ainda buscando metadados do magnet) merece mais paciência: 30 min.
    stalledDL (sem seed disponível AGORA) pode voltar sozinho: 2 h.
    Os valores nunca ficam abaixo do STALL_TIMEOUT_MINUTES configurado.
    """
    s = state.lower()
    if "metadl" in s:       # metaDL / forcedMetaDL
        return max(config.STALL_TIMEOUT_MINUTES, 30)
    if "stalleddl" in s:    # stalledDL
        return max(config.STALL_TIMEOUT_MINUTES, 120)
    return config.STALL_TIMEOUT_MINUTES


async def _wait_downloads(job: dict) -> dict:
    """Espera os torrents necessários terminarem; watchdog troca torrent travado.

    Perder a conexão com o qBittorrent NUNCA falha o job: avisa uma vez,
    mantém o estado "baixando" e fica tentando reconectar. Torrent que sumiu
    do qBittorrent é reinserido automaticamente — só para de reinserir se o
    job for cancelado/excluído.
    """
    needed = _needed_torrents(job)
    paths = {}
    stall = {k: {"pct": -1.0, "since": time.monotonic(), "warned": False, "hash": None}
             for k in needed}
    # magnet adicionado pode levar alguns segundos ate aparecer em /info
    # (qBittorrent ainda buscando metadados). so tratamos como "removido" se
    # sumir por um bom tempo, nao na primeira consulta.
    missing = {k: {"since": None} for k in needed}
    METADATA_GRACE = max(config.STALL_TIMEOUT_MINUTES, 5) * 60
    conn_lost = False
    while len(paths) < len(needed):
        try:
            for kind in needed:
                if kind in paths:
                    continue
                torrents = await _qbit.info_by_tag(_tag(job, kind))
                if conn_lost:
                    conn_lost = False
                    _event(job, "qbit", "✅ Conexão com o qBittorrent restabelecida")
                    job["detail"] = ("Baixando torrent..." if len(needed) == 1
                                     else "Baixando torrents...")
                    # o tempo desconectado não conta como stall nem como sumiço
                    now = time.monotonic()
                    for st_ in stall.values():
                        st_["since"] = now
                    for m_ in missing.values():
                        m_["since"] = None
                if not torrents:
                    miss = missing[kind]
                    if miss["since"] is None:
                        miss["since"] = time.monotonic()
                        _event(job, "qbit",
                               f"Torrent de {kind} ainda não aparece no qBittorrent "
                               f"(buscando metadados do magnet)...")
                    elif time.monotonic() - miss["since"] > METADATA_GRACE:
                        # sumiu (removido à mão?) ou magnet nunca materializou:
                        # reinsere e recomeça a espera, sem falhar o job
                        await _readd_torrent(job, kind)
                        miss["since"] = time.monotonic()
                    continue
                missing[kind]["since"] = None
                t = torrents[0]
                pct = t.get("progress", 0)
                job["progress"][kind] = {
                    "pct": round(pct * 100, 1),
                    "speed": t.get("dlspeed", 0),
                    "eta": t.get("eta"),
                    "state": t.get("state"),
                    "seeds": t.get("num_seeds", 0),
                    "name": t.get("name"),
                }
                if pct >= 1:
                    paths[kind] = t["content_path"]
                    _event(job, "qbit", f"Download de {kind} concluído: {t['content_path']}")
                    continue

                st = stall[kind]
                state = t.get("state") or ""
                if t.get("hash") != st["hash"]:
                    # torrent trocado (watchdog ou troca manual): zera o relógio
                    st.update(hash=t.get("hash"), pct=-1.0,
                              since=time.monotonic(), warned=False)
                limit_min = _stall_limit_minutes(state)
                if state in ("stoppedDL", "pausedDL"):
                    # usuário parou o torrent manualmente: não conta o tempo de
                    # stall (o relógio recomeça do zero quando ele retomar)
                    st.update(since=time.monotonic(), warned=False)
                elif pct > st["pct"] + 1e-4:
                    st.update(pct=pct, since=time.monotonic(), warned=False)
                elif (config.STALL_TIMEOUT_MINUTES > 0
                      and time.monotonic() - st["since"] > limit_min * 60):
                    if await _switch_torrent(job, kind, t, limit_min):
                        st.update(pct=-1.0, since=time.monotonic(), warned=False)
                    elif not st["warned"]:
                        _event(job, "qbit",
                               f"⚠️ Download de {kind} sem progresso há "
                               f"{limit_min} min e sem candidato reserva — "
                               f"continuando a esperar (cancele o job se quiser desistir)")
                        st["warned"] = True
            store.upsert_job(job)
        except _CONN_ERRORS as e:
            # qBittorrent fora do ar / rede caiu: avisa uma vez e segue tentando
            if not conn_lost:
                conn_lost = True
                _event(job, "qbit",
                       f"⚠️ Perdi a conexão com o qBittorrent ({type(e).__name__}: {e}) — "
                       f"mantendo o download e tentando reconectar...")
            job["detail"] = "Sem conexão com o qBittorrent — tentando reconectar..."
        if len(paths) < len(needed):
            await asyncio.sleep(config.POLL_INTERVAL_SECONDS)
    return paths


async def _readd_torrent(job: dict, kind: str):
    """Reinsere o torrent atual de `kind` que sumiu do qBittorrent."""
    cand = (job.get("current") or {}).get(kind)
    if not cand:
        # jobs antigos (sem "current"): tenta achar o candidato pelo título escolhido
        title = (job.get(f"{kind}_torrent") or {}).get("title")
        cands = (job.get("search") or {}).get(kind) or []
        cand = next((c for c in cands if c["title"] == title), None)
    if not cand or not (cand.get("magnet") or cand.get("link")):
        _event(job, "qbit",
               f"⚠️ Torrent de {kind} sumiu do qBittorrent e não tenho o link para "
               f"reinserir — continuando a esperar")
        return
    save_path = job.get("torrent_save_path") or config.QBIT_SAVE_PATH or None
    await _qbit.add(cand.get("magnet") or cand["link"], _tag(job, kind), save_path)
    _event(job, "qbit",
           f"🔁 Torrent de {kind} não está mais no qBittorrent — reinserido: {cand['title']}")


async def _switch_torrent(job: dict, kind: str, current: dict, limit_min: int) -> bool:
    """Watchdog: troca um download travado pelo próximo candidato do mesmo corte."""
    fallbacks = (job.get("fallbacks") or {}).get(kind) or []
    if not fallbacks:
        return False
    nxt = fallbacks[0]
    await _replace_torrent(job, kind, current, nxt,
                           f"⏳ Download de {kind} travado há {limit_min} min — trocado por")
    return True


async def _replace_torrent(job: dict, kind: str, current: dict | None, nxt: dict, reason: str):
    """Substitui o torrent ativo de `kind` por `nxt` (watchdog ou troca manual).

    `current` é o torrent como reportado pelo qBittorrent (pode ser None se ele
    nem chegou a aparecer). O candidato substituído volta para o FIM da lista
    de reservas — dá para tentar de novo mais tarde.
    """
    if not job.get("fallbacks"):
        job["fallbacks"] = {}
    fb = job["fallbacks"].get(kind) or []
    job["fallbacks"][kind] = [c for c in fb if c["id"] != nxt["id"]]
    cur_cand = (job.get("current") or {}).get(kind)
    if cur_cand and cur_cand["id"] != nxt["id"]:
        job["fallbacks"][kind].append(cur_cand)

    tag = _tag(job, kind)
    if current:
        # se o mesmo torrent serve para os dois papéis (dual audio), só tira a tag
        other = "audio" if kind == "video" else "video"
        shared = False
        if other in _needed_torrents(job):
            other_torrents = await _qbit.info_by_tag(_tag(job, other))
            shared = bool(other_torrents) and other_torrents[0].get("hash") == current.get("hash")
        await _qbit.remove_tag(current["hash"], tag)
        if not shared:
            await _qbit.delete(current["hash"], delete_files=True)
    save_path = job.get("torrent_save_path") or config.QBIT_SAVE_PATH or None
    await _qbit.add(nxt.get("magnet") or nxt["link"], tag, save_path)

    job[f"{kind}_torrent"] = {"title": nxt["title"], "seeders": nxt["seeders"],
                              "size": nxt["size"], "score": nxt["score"],
                              "edition": nxt["edition"]}
    if not job.get("current"):
        job["current"] = {}
    job["current"][kind] = nxt
    _event(job, "qbit", f"{reason}: {nxt['title']} ({nxt['seeders']} seeds)")


def _map_qbit_path(job: dict, path: str) -> Path:
    """Traduz o caminho reportado pelo qBittorrent para o caminho local.

    Prioridade: o par save_path->local_path do destino de torrents do job;
    depois o QBIT_PATH_MAP global do .env (fallback/compatibilidade).
    """
    save = job.get("torrent_save_path") or ""
    local = job.get("torrent_local_path") or ""
    if save and local:
        mapped = config.map_path(path, [(save, local)])
        if mapped != path:
            return Path(mapped)
    return Path(config.map_path(path, config.QBIT_PATH_MAP))


def _find_video_file(job: dict, content_path: str) -> Path:
    p = _map_qbit_path(job, content_path)
    if not p.exists():
        raise RuntimeError(
            f"Caminho '{p}' (qBittorrent reportou '{content_path}') não existe nesta máquina. "
            f"Configure o caminho local do destino de torrents em Configurações "
            f"(ou monte a pasta de downloads nesta máquina).")
    if p.is_file():
        return p
    files = [f for f in p.rglob("*")
             if f.suffix.lower() in VIDEO_EXTENSIONS and "sample" not in f.name.lower()]
    if not files:
        raise RuntimeError(f"Nenhum arquivo de vídeo encontrado em {p}")
    return max(files, key=lambda f: f.stat().st_size)


async def _deliver_single(job: dict, files: dict):
    """Job de um torrent só: entrega o arquivo direto no destino, sem merge."""
    kind = "video" if "video" in files else "audio"
    src_file = files[kind]
    _event(job, "info", f"Arquivo baixado: {src_file}")

    movie = job["movie"]
    safe_title = re.sub(r'[<>:"/\\|?*]', "", f"{movie['original_title']} ({movie['year']})")
    tag = "orig" if job["kind"] == "original" else job["language"]
    dest_dir = Path(job.get("destination_path") or config.OUTPUT_DIR)
    output = dest_dir / safe_title / f"{safe_title} [{tag}]{src_file.suffix}"

    label = "original" if job["kind"] == "original" else f"dublado ({job['language']})"
    _set(job, "merging", f"Entregando arquivo {label} no destino...")

    notes: list[str] = []
    # hardlink (fallback cópia) roda em thread para não travar a API em cópias grandes
    await asyncio.to_thread(merger._link_or_copy, src_file, output, notes)
    for n in notes:
        _event(job, "info", n)

    job["output"] = str(output)
    _set(job, "done", f"Concluído — {label} entregue em: {output}")
    await _cleanup_torrents(job)


async def _merge(job: dict, video_file: Path, audio_file: Path):
    _event(job, "merge", f"Arquivo de vídeo: {video_file}")
    _event(job, "merge", f"Arquivo de áudio: {audio_file}")

    movie = job["movie"]
    safe_title = re.sub(r'[<>:"/\\|?*]', "", f"{movie['original_title']} ({movie['year']})")
    # subpasta por filme dentro do destino escolhido (bom para Jellyfin/Plex)
    dest_dir = Path(job.get("destination_path") or config.OUTPUT_DIR)
    output = dest_dir / safe_title / f"{safe_title} [{job['language']}+orig].mkv"

    _set(job, "merging", f"Fazendo merge ({video_file.name} + {audio_file.name})...")

    def log(msg):
        job["detail"] = str(msg)
        _event(job, "merge", str(msg))

    # progresso do ffmpeg: atualiza em memória (a UI le via polling); persiste
    # no banco só de vez em quando para não martelar o SQLite a cada tick
    last_persist = [0.0]

    def on_progress(info: dict):
        job["progress"]["merge"] = info
        now = time.monotonic()
        if now - last_persist[0] > 15:
            last_persist[0] = now
            store.upsert_job(job)

    # merger.merge é bloqueante (ffmpeg/ffprobe); roda em thread para não travar a API
    result = await asyncio.to_thread(
        merger.merge, str(video_file), str(audio_file), str(output),
        job["language"], log=log, on_progress=on_progress)
    job["progress"]["merge"] = None  # terminou (com sucesso): some a barra

    job["output"] = result.output
    if result.linked:
        _set(job, "done", f"Áudio no idioma alvo já existia no melhor vídeo — hardlink criado: {result.output}")
    else:
        _set(job, "done", f"Concluído (offset {result.offset_ms:+.2f} ms): {result.output}")

    await _cleanup_torrents(job)


async def _cleanup_torrents(job: dict):
    if config.QBIT_CLEANUP == "keep":
        return
    # hardlink/cópia são independentes do arquivo do qBittorrent (nada de symlink),
    # então remove_data pode apagar os dados com segurança mesmo quando só linkou.
    delete_files = config.QBIT_CLEANUP == "remove_data"
    for kind in ("video", "audio"):
        try:
            await _qbit.delete_by_tag(_tag(job, kind), delete_files)
            _event(job, "qbit", f"Torrent de {kind} removido do qBittorrent"
                                + (" (com os dados)" if delete_files else ""))
        except Exception as e:  # noqa: BLE001
            _event(job, "qbit", f"Falha ao remover torrent de {kind}: {e}")
