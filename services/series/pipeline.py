"""Orquestrador de jobs de SÉRIE: TMDB TV -> Jackett -> qBittorrent.

Desacoplado do pipeline de filmes (services/jobs.py) — daqui só usamos o
REGISTRO compartilhado de jobs (_jobs, _set, _event, _spawn, tags de limpeza)
e utilitários folha. A regra central, decidida com o usuário: NADA é resolvido
automaticamente em conflito. Todo ponto suspeito pausa o job num gate
explícito (job["awaiting"] = {reason, payload}) e espera o usuário decidir
via POST /api/jobs/{id}/resolve.

Gates:
- manual_pick:            modo manual — escolher torrent por episódio/papel
- gaps_confirm:           episódios sem torrent em um (ou nos dois) idiomas
- incompatible_torrents:  sinais de ordem/divisão de episódios incompatível
- (alignment_review e drift chegam com o motor de alinhamento)

Um job cobre N episódios (request.seasons inteiras + request.episodes
avulsos). Cada episódio tem subestado próprio — o job só é all-or-nothing na
busca; do download em diante falhas são por episódio.
"""
import asyncio
import time
import uuid
from datetime import date, datetime
from pathlib import Path

import config
from services import jackett, jobs, store, tmdb, transcode
from services import selector
from services.series import parse, selector_tv

ROLES = ("original", "dubbed")
ROLE_LABEL = {"original": "original", "dubbed": "dublado"}
# modo do selector por papel (mesma semântica de filmes: video=imagem,
# audio=exige dublagem)
ROLE_MODE = {"original": "video", "dubbed": "audio"}
MAX_PER_EPISODE = 20  # candidatos guardados por episódio×papel (UI/manual)

# pools de resultados do Jackett por job, SÓ em memória: os gates pausam o job
# e o resolve reaproveita a busca. Depois de um restart o pool não existe mais
# — o resolve que precisar dele refaz a busca (Jackett é lento, mas correto
# vem primeiro).
_pools: dict[str, dict[str, list[dict]]] = {}


def ep_key(season: int, episode: int) -> str:
    return f"S{season:02d}E{episode:02d}"


def parse_ep_key(key: str) -> tuple[int, int]:
    s, e = key[1:].split("E")
    return int(s), int(e)


def _is_future(air_date: str | None) -> bool:
    """Sem data ou data à frente = ainda não lançado (exceção da regra de
    cobertura total)."""
    if not air_date:
        return True
    try:
        return date.fromisoformat(air_date) > date.today()
    except ValueError:
        return True


# -------------------- criação --------------------

async def create_series(tmdb_id: int, language: str,
                        seasons: list[int] | None = None,
                        episodes: dict | None = None,
                        mode: str = "auto",
                        destination_id: int | None = None,
                        torrent_target_id: int | None = None,
                        convert: dict | None = None) -> dict:
    """Cria um job de série cobrindo temporadas inteiras e/ou episódios
    avulsos ({"3": [1, 5]}). Sempre baixa os DOIS lados (original + dublado)
    de cada episódio — é a razão de ser do serviço."""
    seasons = sorted({int(s) for s in (seasons or [])})
    episodes = {int(s): sorted({int(e) for e in eps})
                for s, eps in (episodes or {}).items() if eps}
    if not seasons and not episodes:
        raise ValueError("Selecione ao menos uma temporada ou episódio")
    if language not in config.LANGUAGES:
        raise ValueError(f"Idioma inválido: {language!r}")
    if mode not in ("auto", "manual"):
        raise ValueError(f"Modo inválido: {mode!r}")
    if convert is not None:
        convert = transcode.validate(convert).to_dict()

    dest = store.get_destination(destination_id) if destination_id else None
    if dest is None:
        dest = store.default_destination()
    if dest is None:
        raise ValueError("Nenhum destino cadastrado — cadastre uma pasta de destino antes")
    target = store.get_torrent_target(torrent_target_id) if torrent_target_id else None
    if target is None:
        target = store.default_torrent_target()

    job = {
        "id": uuid.uuid4().hex[:10],
        "media_type": "tv",
        "tmdb_id": tmdb_id,
        "language": language,
        "mode": mode,
        "kind": "series",
        "download_only": False,
        "convert": convert,
        "status": "searching",
        "detail": "Buscando informações da série...",
        "movie": None,  # mesmo nome do pipeline de filmes: título/poster na UI
        "request": {"seasons": seasons, "episodes": episodes},
        "episodes": {},        # ep_key -> subestado (ver _prepare)
        "torrents": [],        # torrents planejados/ativos (as duas línguas)
        "awaiting": None,      # gate ativo {reason, payload} ou None
        "order_map": None,     # ep_key -> [s, e] na ordem alternativa escolhida
        "report": None,
        "progress": {},        # merge (M4+); download agregado vem dos torrents
        "output": None,
        "search_tv": None,     # candidatos por papel/episódio (manual/trocas)
        "destination_id": dest["id"], "destination_label": dest["label"],
        "destination_path": dest["path"],
        "torrent_target_id": target["id"] if target else None,
        "torrent_target_label": target["label"] if target else None,
        "torrent_save_path": (target["save_path"] if target else "") or "",
        "torrent_local_path": (target["local_path"] if target else "") or "",
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    jobs._jobs[job["id"]] = job
    scope = []
    if seasons:
        scope.append("temporada(s) " + ", ".join(f"S{s:02d}" for s in seasons))
    if episodes:
        scope.append("episódio(s) " + ", ".join(
            ep_key(s, e) for s, eps in episodes.items() for e in eps))
    conv = transcode.describe(convert)
    cinfo = f" — conversão: {', '.join(conv)}" if conv else ""
    jobs._event(job, "status",
                f"Job de série criado ({' + '.join(scope)}) — destino: "
                f"{dest['label']}{cinfo}")
    jobs._spawn(job["id"], _run(job))
    return jobs._public(job)


# -------------------- pipeline --------------------

async def _run(job: dict):
    try:
        await _prepare(job)
        await _search_series(job)
        await _plan_and_gate(job)
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001 — job nunca derruba o servidor
        jobs._fail(job, f"{type(e).__name__}: {e}")


async def _prepare(job: dict):
    """Metadados TMDB + mapa de episódios pedidos (com skipped_future)."""
    lang = job["language"]
    info = await tmdb.tv_details(job["tmdb_id"], lang)
    job["movie"] = {k: info[k] for k in
                    ("id", "original_title", "localized_title", "english_title",
                     "original_language", "year", "overview", "poster")}
    known = {s["season"] for s in info["seasons"]
             if s["season"] and s["season"] > 0}
    job["known_seasons"] = sorted(known)

    req = job["request"]
    bad = [s for s in req["seasons"] if s not in known]
    bad += [s for s in req["episodes"] if s not in known]
    if bad:
        raise ValueError("Temporada(s) inexistente(s) no TMDB: "
                         + ", ".join(f"S{s:02d}" for s in sorted(set(bad))))

    # temporadas a consultar: inteiras pedidas + as dos episódios avulsos
    wanted: dict[int, set[int] | None] = {s: None for s in req["seasons"]}
    for s, eps in req["episodes"].items():
        if s not in wanted:  # temporada inteira já cobre os avulsos dela
            wanted[s] = set(eps)

    n_future = 0
    for season, only in sorted(wanted.items()):
        data = await tmdb.tv_season(job["tmdb_id"], season)
        for ep in data["episodes"]:
            e = ep["episode"]
            if only is not None and e not in only:
                continue
            key = ep_key(season, e)
            future = _is_future(ep["air_date"])
            n_future += 1 if future else 0
            job["episodes"][key] = {
                "season": season, "episode": e,
                "name": ep["name"], "air_date": ep["air_date"],
                "runtime": ep["runtime"],
                "state": "skipped_future" if future else "pending",
                "src": {}, "output": None, "error": None,
            }
    if not job["episodes"]:
        raise ValueError("Nenhum episódio encontrado para a seleção pedida")
    if all(v["state"] == "skipped_future" for v in job["episodes"].values()):
        raise ValueError("Todos os episódios pedidos ainda não foram lançados")
    if n_future:
        jobs._event(job, "info",
                    f"{n_future} episódio(s) ainda sem data de lançamento "
                    f"passada — ficam de fora desta execução (skipped_future)")

    # ordens alternativas conhecidas (sinal para o gate de incompatibilidade)
    try:
        job["episode_groups"] = await tmdb.tv_episode_groups(job["tmdb_id"])
    except Exception:  # noqa: BLE001 — melhor sem o sinal do que sem o job
        job["episode_groups"] = []

    title = job["movie"]["original_title"]
    jobs._event(job, "info",
                f"Série: {title} ({job['movie']['year']}) — "
                f"{len(_active_refs(job))} episódio(s) a baixar")


def _active_refs(job: dict) -> list[tuple[int, int]]:
    """Episódios que o job ainda pretende baixar (fora os skipped_*)."""
    return [(v["season"], v["episode"]) for v in job["episodes"].values()
            if v["state"] not in ("skipped_future", "skipped_missing")]


def _search_ref(job: dict, key: str) -> tuple[int, int]:
    """Referência do episódio NO NOME DOS RELEASES: a ordem alternativa
    escolhida no gate (order_map), ou a ordem de exibição do TMDB."""
    om = job.get("order_map") or {}
    if key in om:
        return tuple(om[key])
    return parse_ep_key(key)


async def _search_series(job: dict):
    """Busca no Jackett (categoria TV) para os dois papéis, em paralelo.

    Fase 1: por temporada ("Título S01", "Título temporada 1") + título puro
    (pega packs "Complete"/numeração fora do padrão). A fase 2 (por episódio,
    só para lacunas) roda dentro do _plan."""
    lang = job["language"]
    label = config.LANGUAGES[lang]["label"]
    m = job["movie"]
    original, localized = m["original_title"], m["localized_title"]
    english = m.get("english_title")
    has_localized = bool(localized and localized.lower() != (original or "").lower())

    titles_original = [t for t in (original, english) if t]
    loc_spellings = ([localized] + selector.title_variants(localized, include_and=False)
                     if has_localized else [])

    search_seasons = sorted({s for s, _ in _active_refs(job)})
    if job.get("order_map"):
        search_seasons = sorted({_search_ref(job, k)[0]
                                 for k in job["episodes"]
                                 if job["episodes"][k]["state"] not in
                                 ("skipped_future", "skipped_missing")})

    def season_queries(titles: list[str], portuguese: bool) -> list[str]:
        out, seen = [], set()

        def add(q: str):
            if q.lower() not in seen:
                seen.add(q.lower())
                out.append(q)
        for t in titles:
            add(t)  # título puro: packs "Complete", numeração absoluta etc.
            for s in search_seasons:
                add(f"{t} S{s:02d}")
                if portuguese:
                    add(f"{t} temporada {s}")
        return out

    q_original = season_queries(titles_original, portuguese=False)
    # sem título localizado distinto, o lado dublado busca pelos mesmos nomes
    # (o marcador de idioma filtra depois)
    q_dubbed = season_queries(loc_spellings or titles_original, portuguese=True)

    jobs._set(job, "searching",
              f"Procurando série no Jackett ({len(q_original) + len(q_dubbed)} "
              f"busca(s), categoria TV)...")

    async def run(query: str) -> list[dict]:
        try:
            res = await jackett.search(query, categories=jackett.TV_CATEGORIES)
            jobs._event(job, "search", f"'{query}' → {len(res)} resultados")
            return res
        except Exception as e:  # noqa: BLE001 — uma busca não derruba as outras
            jobs._event(job, "search",
                        f"⚠️ Busca '{query}' falhou: {type(e).__name__}: {e}")
            return []

    all_res = await asyncio.gather(*(run(q) for q in q_original + q_dubbed))
    pool_original = jobs._dedup_results(
        [r for res in all_res[:len(q_original)] for r in res])
    pool_dubbed = jobs._dedup_results(
        [r for res in all_res[len(q_original):] for r in res])
    _pools[job["id"]] = {"original": pool_original, "dubbed": pool_dubbed}
    jobs._event(job, "search",
                f"Pool: {len(pool_original)} resultados (original), "
                f"{len(pool_dubbed)} ({label})")


def _rank_all(job: dict) -> dict[str, dict[tuple[int, int], list[dict]]]:
    """Ranking por episódio×papel a partir dos pools. Também preenche
    job["search_tv"] (candidatos enxutos para a UI/manual/trocas)."""
    lang = job["language"]
    m = job["movie"]
    original, localized = m["original_title"], m["localized_title"]
    english = m.get("english_title")
    has_localized = bool(localized and localized.lower() != (original or "").lower())
    titles_original = [t for t in (original, english) if t]
    titles_dubbed = titles_original + ([localized] if has_localized else [])
    dubbed_title = localized if has_localized else None
    known = set(job.get("known_seasons") or [])
    pools = _pools.get(job["id"]) or {"original": [], "dubbed": []}

    refs = _active_refs(job)
    search_refs = {ep_key(s, e): _search_ref(job, ep_key(s, e)) for s, e in refs}
    ranked_all: dict[str, dict] = {}
    job["search_tv"] = {"original": {}, "dubbed": {}}
    for role in ROLES:
        titles = titles_original if role == "original" else titles_dubbed
        ranked_all[role] = {}
        for s, e in refs:
            key = ep_key(s, e)
            ss, se = search_refs[key]
            ranked, trace = selector_tv.rank_for_episode(
                pools[role], ROLE_MODE[role], titles, m["year"], ss, se,
                language=lang,
                require_language=(role == "dubbed"),
                dubbed_title=dubbed_title,
                requested=[search_refs[ep_key(a, b)] for a, b in refs],
                known_seasons=known)
            ranked_all[role][(s, e)] = ranked
            job["search_tv"][role][key] = [
                _slim(c, f"{key}:{role[0]}{i}")
                for i, c in enumerate(ranked[:MAX_PER_EPISODE])]
    return ranked_all


def _slim(cand: dict, cid: str) -> dict:
    return {"id": cid, "title": cand["title"], "tracker": cand.get("tracker"),
            "seeders": cand["seeders"], "size": cand["size"],
            "quality": cand.get("quality"), "coverage": cand.get("coverage"),
            "score": cand.get("score"), "tier": cand.get("tier"),
            "magnet": cand.get("magnet"), "link": cand.get("link"),
            "infohash": cand.get("infohash"), "files": cand.get("files")}


async def _plan_and_gate(job: dict):
    """Monta o plano de cobertura, roda a fase 2 para lacunas e passa pelos
    gates (manual → lacunas → incompatibilidade) antes do download.

    Reentra depois de cada gate. O plano (job["torrents"]) só é construído
    quando não existe — as decisões dos gates (troca, escolha manual, lacunas
    aceitas) MUTAM o plano existente e não podem ser descartadas por um
    replanejamento."""
    if not job["torrents"]:
        if job["id"] not in _pools:
            # pool perdido (restart/remap): refaz a busca antes de planejar
            await _search_series(job)
        jobs._set(job, "searching", "Avaliando candidatos por episódio...")
        ranked_all = _rank_all(job)

        # fase 2: episódios sem candidato em algum papel ganham buscas dedicadas
        missing = {role: [ref for ref in _active_refs(job)
                          if not ranked_all[role].get(ref)]
                   for role in ROLES}
        if any(missing.values()) and not job.get("_phase2_done"):
            job["_phase2_done"] = True
            await _phase2_search(job, missing)
            ranked_all = _rank_all(job)

        plans = {role: selector_tv.build_plan(
            [_search_ref(job, ep_key(s, e)) for s, e in _active_refs(job)],
            {(_search_ref(job, ep_key(s, e))): ranked_all[role][(s, e)]
             for s, e in _active_refs(job)},
            known_seasons=set(job.get("known_seasons") or []))
            for role in ROLES}
        _store_plan(job, plans)

    if job["mode"] == "manual" and not job.get("_manual_done"):
        _gate(job, "manual_pick", {
            "candidates": job["search_tv"],
            "preselected": _plan_summary(job),
        }, "Escolha os torrents por episódio (modo manual)")
        return

    gaps = _collect_gaps(job)
    if gaps and not job.get("_gaps_accepted"):
        _gate(job, "gaps_confirm", {"missing": gaps},
              f"⚠️ {len(gaps)} episódio(s) sem torrent — decidir antes de baixar")
        return

    signals = _collect_signals(job)
    if signals and not job.get("_signals_accepted"):
        _gate(job, "incompatible_torrents", {
            "torrents": signals,
            "episode_groups": job.get("episode_groups") or [],
        }, "⚠️ Torrent(s) com sinal de incompatibilidade — confirme antes de baixar")
        return

    await _start_downloads(job)


async def _phase2_search(job: dict, missing: dict[str, list]):
    """Buscas dedicadas por episódio para papéis sem candidato (fase 2)."""
    m = job["movie"]
    localized = m["localized_title"]
    has_localized = bool(localized and localized.lower()
                         != (m["original_title"] or "").lower())
    queries: list[tuple[str, str]] = []  # (role, query)
    for role, refs in missing.items():
        titles = ([m["original_title"]] if role == "original"
                  else [localized if has_localized else m["original_title"]])
        for s, e in refs:
            ss, se = _search_ref(job, ep_key(s, e))
            for t in titles:
                if t:
                    queries.append((role, f"{t} {ep_key(ss, se)}"))
    if not queries:
        return
    jobs._event(job, "search",
                f"Fase 2: {len(queries)} busca(s) por episódio para as lacunas")

    async def run(q: str) -> list[dict]:
        try:
            return await jackett.search(q, categories=jackett.TV_CATEGORIES)
        except Exception:  # noqa: BLE001
            return []

    res = await asyncio.gather(*(run(q) for _, q in queries))
    pools = _pools.setdefault(job["id"], {"original": [], "dubbed": []})
    for (role, q), r in zip(queries, res):
        if r:
            jobs._event(job, "search", f"Fase 2 '{q}' → {len(r)} resultados")
        pools[role] = jobs._dedup_results(pools[role] + r)


def _store_plan(job: dict, plans: dict):
    """Materializa o plano em job["torrents"] (persistido — o download e o
    resume dependem só disso, não dos pools em memória)."""
    torrents = []
    n = 0
    for role in ROLES:
        for t in plans[role]["torrents"]:
            entry = {
                "n": n, "role": role,
                "tag": f"dl-{job['id']}-t{n}",
                "title": t["title"], "tracker": t.get("tracker"),
                "seeders": t.get("seeders"), "size": t.get("size"),
                "quality": t.get("quality"), "coverage_label": t.get("coverage"),
                "magnet": t.get("magnet"), "link": t.get("link"),
                "infohash": t.get("infohash"), "files_count": t.get("files"),
                # episódios (chave AIRED) que este torrent atende
                "coverage": [ep_key(*_aired_ref(job, ref))
                             for ref in t.get("coverage_assigned") or []],
                "state": "pending", "hash": None, "progress": None,
                "selected_files": None, "content_path": None,
            }
            torrents.append(entry)
            n += 1
    job["torrents"] = torrents
    store.upsert_job(job)


def _aired_ref(job: dict, search_ref: tuple[int, int]) -> tuple[int, int]:
    """Inverso de _search_ref: da referência de busca (ordem alternativa) para
    a chave de exibição usada em job["episodes"]."""
    om = job.get("order_map") or {}
    for key, alt in om.items():
        if tuple(alt) == tuple(search_ref):
            return parse_ep_key(key)
    return search_ref


def _plan_summary(job: dict) -> list[dict]:
    return [{"n": t["n"], "role": t["role"], "title": t["title"],
             "quality": t.get("quality"), "coverage": t["coverage"]}
            for t in job["torrents"]]


def _collect_gaps(job: dict) -> list[dict]:
    """Episódios pedidos sem torrent em um (ou nos dois) papéis. TODOS passam
    pelo gate — a regra do produto é nunca decidir lacuna sozinho."""
    covered = {role: {ep for t in job["torrents"] if t["role"] == role
                      for ep in t["coverage"]} for role in ROLES}
    gaps = []
    for s, e in _active_refs(job):
        key = ep_key(s, e)
        roles_missing = [ROLE_LABEL[r] for r in ROLES if key not in covered[r]]
        if roles_missing:
            gaps.append({"episode": key,
                         "name": job["episodes"][key].get("name"),
                         "missing": roles_missing})
    return gaps


def _collect_signals(job: dict) -> list[dict]:
    """Sinais de incompatibilidade nos torrents ESCOLHIDOS (o gate mostra as
    alternativas de cada um)."""
    out = []
    counts = _episode_counts_by_season(job)
    groups = job.get("episode_groups") or []
    for t in job["torrents"]:
        cov = parse.parse_coverage(t["title"])
        tmdb_count = None
        if cov.kind in ("season_pack", "complete"):
            seasons = cov.seasons or set(job.get("known_seasons") or [])
            tmdb_count = sum(counts.get(s, 0) for s in seasons) or None
        signals = parse.suspicious_signals(
            t["title"], files_count=t.get("files_count"),
            tmdb_episode_count=tmdb_count,
            # ordens alternativas só reforçam sinal quando o nome já é ambíguo
            alt_order_groups=groups if parse.suspicious_signals(t["title"]) else None)
        if signals:
            out.append({"n": t["n"], "role": t["role"], "title": t["title"],
                        "coverage": t["coverage"], "signals": signals,
                        "alternatives": _alternatives_for(job, t)})
    return out


def _episode_counts_by_season(job: dict) -> dict[int, int]:
    """Episódios por temporada — só das temporadas que o job consultou."""
    counts: dict[int, int] = {}
    for v in job["episodes"].values():
        counts[v["season"]] = counts.get(v["season"], 0) + 1
    return counts


def _alternatives_for(job: dict, torrent: dict) -> list[dict]:
    """Candidatos alternativos que cobrem TODOS os episódios deste torrent."""
    search = (job.get("search_tv") or {}).get(torrent["role"]) or {}
    if not torrent["coverage"]:
        return []
    first = search.get(torrent["coverage"][0]) or []
    out = []
    for c in first:
        if c["title"] == torrent["title"]:
            continue
        cov = parse.parse_coverage(c["title"])
        known = set(job.get("known_seasons") or [])
        if all(cov.covers(*_search_ref(job, k), known)
               for k in torrent["coverage"]):
            out.append(c)
    return out[:10]


# -------------------- gates --------------------

def _gate(job: dict, reason: str, payload: dict, detail: str):
    """Pausa o job num gate explícito. A coroutine do pipeline RETORNA depois
    de chamar isto; o resolve() respawna a continuação."""
    job["awaiting"] = {"reason": reason, "payload": payload}
    jobs._set(job, "awaiting", detail)


async def resolve(job_id: str, reason: str, decision: dict) -> dict | None:
    """Resolve o gate ativo (ou o force_continue durante o download).

    Decisões por gate:
    - manual_pick:           {picks: {ep_key: {original: id, dubbed: id}}}
    - gaps_confirm:          {accept: bool}
    - incompatible_torrents: {action: "accept"}
                             {action: "replace", torrent_n, candidate_id}
                             {action: "remap", group_id}
    - force_continue:        {} (aceito com o job em downloading)
    """
    job = jobs._jobs.get(job_id)
    if not job or job.get("media_type") != "tv":
        return None

    if reason == "force_continue":
        if job["status"] != "downloading":
            raise ValueError("Só dá para forçar a próxima etapa durante o download")
        job["force_continue"] = True
        jobs._event(job, "chosen",
                    "Usuário forçou a próxima etapa — episódios não baixados "
                    "serão pulados")
        return jobs._public(job)

    gate = job.get("awaiting")
    if job["status"] != "awaiting" or not gate or gate["reason"] != reason:
        raise ValueError(
            f"O job não está aguardando '{reason}'"
            + (f" (gate atual: {gate['reason']})" if gate else ""))

    job["awaiting"] = None
    if reason == "gaps_confirm":
        if not decision.get("accept"):
            jobs._event(job, "chosen", "Usuário desistiu por causa das lacunas")
            await jobs.cancel(job_id)
            cancelled = jobs._lookup(job_id)
            return jobs._public(cancelled) if cancelled else None
        _apply_gaps(job)
        job["_gaps_accepted"] = True
        jobs._event(job, "chosen", "Usuário aceitou continuar com as lacunas")
    elif reason == "incompatible_torrents":
        action = decision.get("action")
        if action == "accept":
            job["_signals_accepted"] = True
            jobs._event(job, "chosen",
                        "Usuário aceitou os torrents mesmo com sinais de "
                        "incompatibilidade")
        elif action == "replace":
            _apply_replace(job, decision)
        elif action == "remap":
            await _apply_remap(job, decision)
        else:
            job["awaiting"] = gate  # decisão inválida: gate continua de pé
            raise ValueError(f"Ação inválida: {action!r}")
    elif reason == "manual_pick":
        _apply_manual(job, decision)
        job["_manual_done"] = True
    elif reason == "alignment_review":
        try:
            _apply_review(job, decision)
        except ValueError:
            job["awaiting"] = gate  # decisão inválida: gate continua de pé
            raise
        # episódios ainda sem decisão continuam em revisão: re-abre o gate
        remaining = {k: v["edl"] for k, v in job["episodes"].items()
                     if v["state"] == "review"}
        if remaining:
            _gate(job, "alignment_review", {"episodes": remaining},
                  f"⚠️ {len(remaining)} episódio(s) ainda precisam de revisão")
            return jobs._public(job)
        jobs._spawn(job["id"], _resume_merge(job))
        return jobs._public(job)
    else:
        job["awaiting"] = gate
        raise ValueError(f"Gate desconhecido: {reason!r}")

    jobs._spawn(job["id"], _continue_after_gate(job))
    return jobs._public(job)


def _apply_review(job: dict, decision: dict):
    """Aplica as decisões da revisão de alinhamento.

    decision:
    - actions: {ep_key: {índice_do_segmento: ação}} — decisões explícitas
    - rules:   regras reaplicáveis (valem para TODOS os episódios do job e
               ficam salvas em job["review_rules"] para os próximos)
    - skip:    episódios cuja revisão o usuário desistiu (viram failed)
    """
    from services.series.align import edl as edl_mod, rules as rules_mod

    new_rules = rules_mod.validate_rules(decision.get("rules") or [])
    if new_rules:
        job["review_rules"] = (job.get("review_rules") or []) + new_rules
        jobs._event(job, "chosen",
                    f"{len(new_rules)} regra(s) de revisão salva(s) — valem "
                    f"para todos os episódios")
    skip = set(decision.get("skip") or [])
    actions = decision.get("actions") or {}

    for key, ep in job["episodes"].items():
        if ep["state"] != "review":
            continue
        if key in skip:
            ep["state"] = "failed"
            ep["error"] = "revisão de alinhamento pulada pelo usuário"
            continue
        edl_dict = ep.get("edl") or {}
        segs_raw = edl_dict.get("segments") or []
        for idx_str, action in (actions.get(key) or {}).items():
            idx = int(idx_str)
            if not 0 <= idx < len(segs_raw):
                raise ValueError(f"{key}: segmento {idx} não existe")
            if action not in rules_mod.VALID_ACTIONS:
                raise ValueError(f"{key}: ação inválida {action!r}")
            segs_raw[idx]["action"] = action
        # reavalia: ações explícitas + regras acumuladas resolvem tudo?
        segs = edl_mod.segments(edl_dict)
        dur_a = edl_dict.get("source_dub", {}).get("duration", 0.0)
        segs, needs = rules_mod.apply_rules(
            segs, job.get("review_rules") or [], dur_a)
        # persiste as ações que as regras acabaram de definir
        for raw, seg in zip(segs_raw, segs):
            if seg.extra.get("action"):
                raw["action"] = seg.extra["action"]
        edl_dict["review"]["required"] = needs
        if not needs:
            ep["state"] = "downloaded"  # volta para a fila: o merge renderiza
            jobs._event(job, "chosen", f"{key}: revisão resolvida")


async def _continue_after_gate(job: dict):
    try:
        await _plan_and_gate(job)
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001
        jobs._fail(job, f"{type(e).__name__}: {e}")


def _apply_gaps(job: dict):
    """Continuar com lacunas: episódios sem cobertura completa viram
    skipped_missing (não contam para a execução, ficam no relatório)."""
    covered = {role: {ep for t in job["torrents"] if t["role"] == role
                      for ep in t["coverage"]} for role in ROLES}
    for s, e in _active_refs(job):
        key = ep_key(s, e)
        if any(key not in covered[r] for r in ROLES):
            job["episodes"][key]["state"] = "skipped_missing"
    # torrents que só existiam por esses episódios saem do plano
    for t in job["torrents"]:
        t["coverage"] = [k for k in t["coverage"]
                         if job["episodes"][k]["state"] not in
                         ("skipped_future", "skipped_missing")]
    job["torrents"] = [t for t in job["torrents"] if t["coverage"]]


def _apply_replace(job: dict, decision: dict):
    """Troca um torrent do plano por um candidato alternativo."""
    n = decision.get("torrent_n")
    cid = decision.get("candidate_id")
    entry = next((t for t in job["torrents"] if t["n"] == n), None)
    if entry is None:
        raise ValueError("Torrent não encontrado no plano")
    cand = None
    for cands in (job.get("search_tv") or {}).get(entry["role"], {}).values():
        cand = next((c for c in cands if c["id"] == cid), None)
        if cand:
            break
    if cand is None:
        raise ValueError("Candidato não encontrado (a busca pode ter sido refeita)")
    entry.update(title=cand["title"], tracker=cand.get("tracker"),
                 seeders=cand.get("seeders"), size=cand.get("size"),
                 quality=cand.get("quality"), magnet=cand.get("magnet"),
                 link=cand.get("link"), infohash=cand.get("infohash"),
                 files_count=cand.get("files"),
                 coverage_label=cand.get("coverage"))
    jobs._event(job, "chosen", f"Torrent trocado pelo usuário: {cand['title']}")
    # a troca pode ter os próprios sinais: o gate roda de novo no continue


async def _apply_remap(job: dict, decision: dict):
    """Usuário escolheu uma ordem alternativa (episode_group do TMDB): os
    episódios pedidos são remapeados para essa ordem e a busca/plano refaz."""
    group_id = decision.get("group_id")
    if not group_id:
        raise ValueError("group_id é obrigatório para remapear")
    detail = await tmdb.tv_episode_group(str(group_id))
    # (temporada, episódio) da ordem de exibição -> posição na ordem alternativa
    order_map: dict[str, list[int]] = {}
    for gi, g in enumerate(detail["groups"], start=1):
        for e in g["episodes"]:
            aired = ep_key(e["season"], e["episode"])
            order_map[aired] = [gi, (e.get("order") or 0) + 1]
    missing = [k for k in job["episodes"]
               if k not in order_map
               and job["episodes"][k]["state"] not in
               ("skipped_future", "skipped_missing")]
    if missing:
        raise ValueError(
            "A ordem escolhida não contém o(s) episódio(s): "
            + ", ".join(missing[:8]))
    job["order_map"] = {k: v for k, v in order_map.items()
                        if k in job["episodes"]}
    # sinais/gates anteriores valem para o plano antigo — recomeça limpo
    job.pop("_signals_accepted", None)
    job.pop("_gaps_accepted", None)
    job.pop("_phase2_done", None)
    job["torrents"] = []          # força replanejar na nova numeração
    _pools.pop(job["id"], None)   # as queries mudam (S01E07 -> S01E05 etc.)
    jobs._event(job, "chosen",
                f"Ordem alternativa aplicada: {detail.get('name')} — "
                f"busca e plano refeitos nessa numeração")


def _apply_manual(job: dict, decision: dict):
    """Aplica as escolhas manuais por episódio/papel por cima do plano
    automático (episódio sem escolha explícita mantém o candidato do plano)."""
    picks = decision.get("picks") or {}
    if not picks:
        jobs._event(job, "chosen", "Modo manual: usuário manteve o plano automático")
        return
    search = job.get("search_tv") or {}
    # agrupa escolhas por candidato (o mesmo pack escolhido para vários
    # episódios vira UM torrent)
    by_ident: dict[tuple[str, str], dict] = {}
    for key, roles in picks.items():
        if key not in job["episodes"]:
            raise ValueError(f"Episódio desconhecido: {key}")
        for role, cid in roles.items():
            if role not in ROLES:
                raise ValueError(f"Papel inválido: {role}")
            cand = next((c for c in (search.get(role, {}).get(key) or [])
                         if c["id"] == cid), None)
            if cand is None:
                raise ValueError(f"Candidato {cid!r} não encontrado para {key}")
            ident = (role, parse.torrent_identity(cand))
            entry = by_ident.setdefault(ident, {"cand": cand, "eps": []})
            entry["eps"].append(key)

    for (role, _ident), info in by_ident.items():
        cand, eps = info["cand"], info["eps"]
        # tira os episódios escolhidos dos torrents do plano automático
        for t in job["torrents"]:
            if t["role"] == role:
                t["coverage"] = [k for k in t["coverage"] if k not in eps]
        existing = next((t for t in job["torrents"] if t["role"] == role
                         and parse.torrent_identity(t) ==
                         parse.torrent_identity(cand)), None)
        if existing:
            existing["coverage"] = sorted(set(existing["coverage"]) | set(eps))
            continue
        n = max((t["n"] for t in job["torrents"]), default=-1) + 1
        job["torrents"].append({
            "n": n, "role": role, "tag": f"dl-{job['id']}-t{n}",
            "title": cand["title"], "tracker": cand.get("tracker"),
            "seeders": cand.get("seeders"), "size": cand.get("size"),
            "quality": cand.get("quality"),
            "coverage_label": cand.get("coverage"),
            "magnet": cand.get("magnet"), "link": cand.get("link"),
            "infohash": cand.get("infohash"), "files_count": cand.get("files"),
            "coverage": sorted(eps), "state": "pending", "hash": None,
            "progress": None, "selected_files": None, "content_path": None,
        })
    job["torrents"] = [t for t in job["torrents"] if t["coverage"]]
    jobs._event(job, "chosen",
                f"Modo manual: {len(picks)} episódio(s) com escolha explícita")
    store.upsert_job(job)


# -------------------- download --------------------

def shared_tag(job: dict) -> str:
    """Tag comum a todos os torrents do job (1 consulta por tick no watchdog;
    também é a tag de limpeza no cancel/remove)."""
    return f"dl-{job['id']}"


async def _start_downloads(job: dict):
    """Envia os torrents planejados ao qBittorrent e entra no watchdog."""
    if not job["torrents"]:
        raise RuntimeError("Plano vazio — nenhum torrent para baixar")
    for key in [k for s, e in _active_refs(job) for k in (ep_key(s, e),)]:
        job["episodes"][key]["state"] = "downloading"
    jobs._set(job, "searching", "Enviando torrents para o qBittorrent...")
    save_path = job.get("torrent_save_path") or config.QBIT_SAVE_PATH or None
    for t in job["torrents"]:
        if t["state"] != "pending":
            continue
        url = t.get("magnet") or t.get("link")
        await jobs._qbit.add(url, f"{shared_tag(job)},{t['tag']}", save_path)
        t["state"] = "downloading"
        jobs._event(job, "qbit",
                    f"Torrent enviado ({ROLE_LABEL[t['role']]}, "
                    f"{len(t['coverage'])} ep.): {t['title']}")
    jobs._set(job, "downloading",
              f"Baixando {len(job['torrents'])} torrent(s)...")
    await _wait_downloads(job)


async def _resume_download(job: dict):
    """Retomada pós-restart: o plano está persistido; os torrents já estão (ou
    serão reinseridos) no qBittorrent — só reentrar no watchdog."""
    try:
        await _wait_downloads(job)
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001
        jobs._fail(job, f"{type(e).__name__}: {e}")


async def _wait_downloads(job: dict):
    """Watchdog: UMA consulta por tick (tag compartilhada), demultiplexada por
    torrent. Mesmas regras do pipeline de filmes: conexão perdida nunca falha o
    job, stall estado-dependente, torrent sumido é reinserido; e as novidades
    de série: seleção de arquivos em packs e force_continue."""
    pending = lambda: [t for t in job["torrents"] if t["state"] == "downloading"]  # noqa: E731
    stall = {t["n"]: {"pct": -1.0, "since": time.monotonic(), "warned": False,
                      "hash": None} for t in job["torrents"]}
    missing = {t["n"]: None for t in job["torrents"]}
    METADATA_GRACE = max(config.STALL_TIMEOUT_MINUTES, 5) * 60
    conn_lost = False
    last_persist = 0.0

    while pending():
        if job.get("force_continue"):
            _apply_force_continue(job)
            break
        try:
            infos = await jobs._qbit.info_by_tag(shared_tag(job))
            if conn_lost:
                conn_lost = False
                jobs._event(job, "qbit", "Conexão com o qBittorrent reestabelecida")
                now = time.monotonic()
                for st in stall.values():
                    st["since"] = now
                for n in missing:
                    missing[n] = None
            by_tag: dict[str, dict] = {}
            for info in infos:
                for tg in (info.get("tags") or "").split(","):
                    by_tag[tg.strip()] = info
            for t in pending():
                info = by_tag.get(t["tag"])
                if info is None:
                    if missing[t["n"]] is None:
                        missing[t["n"]] = time.monotonic()
                        jobs._event(job, "qbit",
                                    f"Torrent t{t['n']} ainda não aparece no "
                                    f"qBittorrent (metadados do magnet?)...")
                    elif time.monotonic() - missing[t["n"]] > METADATA_GRACE:
                        await _readd(job, t)
                        missing[t["n"]] = time.monotonic()
                    continue
                missing[t["n"]] = None
                t["hash"] = info.get("hash")
                await _ensure_file_selection(job, t)
                pct = info.get("progress", 0)
                t["progress"] = {
                    "pct": round(pct * 100, 1), "speed": info.get("dlspeed", 0),
                    "eta": info.get("eta"), "state": info.get("state"),
                    "seeds": info.get("num_seeds", 0), "name": info.get("name"),
                    "size": info.get("size", 0),
                    "downloaded": info.get("completed", 0),
                }
                if pct >= 1:
                    t["state"] = "done"
                    t["content_path"] = info.get("content_path")
                    jobs._event(job, "qbit",
                                f"Torrent t{t['n']} concluído: {info.get('name')}")
                    continue
                st = stall[t["n"]]
                state = info.get("state") or ""
                if info.get("hash") != st["hash"]:
                    st.update(hash=info.get("hash"), pct=-1.0,
                              since=time.monotonic(), warned=False)
                limit_min = jobs._stall_limit_minutes(state)
                if state in ("stoppedDL", "pausedDL"):
                    st.update(since=time.monotonic(), warned=False)
                elif pct > st["pct"] + 1e-4:
                    st.update(pct=pct, since=time.monotonic(), warned=False)
                elif (config.STALL_TIMEOUT_MINUTES > 0
                      and time.monotonic() - st["since"] > limit_min * 60):
                    if await _switch_stalled(job, t, limit_min):
                        st.update(pct=-1.0, since=time.monotonic(), warned=False)
                    elif not st["warned"]:
                        jobs._event(job, "qbit",
                                    f"⚠️ t{t['n']} sem progresso há {limit_min} "
                                    f"min e sem reserva — seguindo esperando")
                        st["warned"] = True
            job["detail"] = _download_detail(job)
            if time.monotonic() - last_persist >= config.PROGRESS_PERSIST_SECONDS:
                store.upsert_job(job)
                last_persist = time.monotonic()
        except jobs._CONN_ERRORS as e:
            if not conn_lost:
                conn_lost = True
                reason = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
                jobs._event(job, "qbit",
                            f"Conexão com o qBittorrent perdida ({reason})")
            job["detail"] = "Sem conexão com o qBittorrent — tentando reconectar..."
        if pending():
            await asyncio.sleep(jobs.poll_interval())

    await _resolve_episode_files(job)
    await _after_download(job)


def _download_detail(job: dict) -> str:
    done = sum(1 for t in job["torrents"] if t["state"] == "done")
    return f"Baixando torrents ({done}/{len(job['torrents'])} concluídos)..."


def _apply_force_continue(job: dict):
    """Forçar a próxima etapa: torrents inacabados são abandonados (ficam no
    qBittorrent) e os episódios que dependiam só deles viram skipped_missing."""
    for t in job["torrents"]:
        if t["state"] == "downloading":
            t["state"] = "abandoned"
    jobs._event(job, "info",
                "⏭️ Próxima etapa forçada — torrents inacabados abandonados "
                "(continuam no qBittorrent)")


async def _ensure_file_selection(job: dict, t: dict):
    """Pack: baixa só os arquivos dos episódios atendidos por este torrent.

    Roda uma vez, quando os metadados chegam. Arquivo sem referência de
    episódio no nome fica FORA (não dá para saber o que é); se nenhum arquivo
    casar, mantém tudo (nome de arquivo fora do padrão — melhor baixar de mais
    do que de menos)."""
    if t["selected_files"] is not None or not t.get("hash"):
        return
    cov = parse.parse_coverage(t["title"])
    if cov.kind in ("episode",):  # torrent de 1 episódio: nada a desmarcar
        t["selected_files"] = []
        return
    files = await jobs._qbit.files(t["hash"])
    if not files:
        return  # metadados ainda não chegaram; tenta no próximo tick
    wanted = {_search_ref(job, k) for k in t["coverage"]}
    keep, drop = [], []
    for f in files:
        name = (f.get("name") or "").rsplit("/", 1)[-1]
        refs = parse.parse_episode_refs(name)
        (keep if any(r in wanted for r in refs) else drop).append(f)
    if not keep:
        t["selected_files"] = []
        jobs._event(job, "qbit",
                    f"⚠️ t{t['n']}: nenhum arquivo do pack casa com os episódios "
                    f"pedidos pelo nome — baixando o pack inteiro")
        return
    await jobs._qbit.file_prio(t["hash"], [f["index"] for f in drop], 0)
    t["selected_files"] = [f["name"] for f in keep]
    jobs._event(job, "qbit",
                f"t{t['n']}: pack com {len(files)} arquivo(s) — baixando "
                f"{len(keep)} ({len(drop)} desmarcados)")


async def _readd(job: dict, t: dict):
    """Reinsere um torrent que sumiu do qBittorrent."""
    url = t.get("magnet") or t.get("link")
    if not url:
        jobs._event(job, "qbit",
                    f"⚠️ t{t['n']} sumiu e não tenho link para reinserir")
        return
    save_path = job.get("torrent_save_path") or config.QBIT_SAVE_PATH or None
    await jobs._qbit.add(url, f"{shared_tag(job)},{t['tag']}", save_path)
    t["selected_files"] = None  # reseleciona os arquivos quando reaparecer
    jobs._event(job, "qbit", f"🔁 t{t['n']} reinserido: {t['title']}")


async def _switch_stalled(job: dict, t: dict, limit_min: int) -> bool:
    """Watchdog: troca um torrent travado por um candidato que cubra os MESMOS
    episódios. Sem reserva compatível, avisa e segue esperando."""
    search = (job.get("search_tv") or {}).get(t["role"]) or {}
    first = search.get(t["coverage"][0]) if t["coverage"] else None
    if not first:
        return False
    known = set(job.get("known_seasons") or [])
    used = {parse.torrent_identity(x) for x in job["torrents"]}
    nxt = None
    for c in first:
        if parse.torrent_identity(c) in used:
            continue
        cov = parse.parse_coverage(c["title"])
        if all(cov.covers(*_search_ref(job, k), known) for k in t["coverage"]):
            nxt = c
            break
    if nxt is None:
        return False
    if t.get("hash"):
        try:
            await jobs._qbit.delete(t["hash"], delete_files=True)
        except jobs._CONN_ERRORS:
            pass
    t.update(title=nxt["title"], tracker=nxt.get("tracker"),
             seeders=nxt.get("seeders"), size=nxt.get("size"),
             quality=nxt.get("quality"), magnet=nxt.get("magnet"),
             link=nxt.get("link"), infohash=nxt.get("infohash"),
             coverage_label=nxt.get("coverage"),
             hash=None, selected_files=None, progress=None)
    save_path = job.get("torrent_save_path") or config.QBIT_SAVE_PATH or None
    await jobs._qbit.add(nxt.get("magnet") or nxt["link"],
                         f"{shared_tag(job)},{t['tag']}", save_path)
    jobs._event(job, "qbit",
                f"⏳ t{t['n']} travado há {limit_min} min — trocado por: "
                f"{nxt['title']}")
    return True


async def _resolve_episode_files(job: dict):
    """Mapeia cada episódio para os arquivos baixados (um por papel)."""
    for t in job["torrents"]:
        if t["state"] != "done" or not t.get("content_path"):
            continue
        try:
            root = await asyncio.to_thread(
                jobs._map_qbit_path, job, t["content_path"])
        except Exception as e:  # noqa: BLE001
            jobs._event(job, "info",
                        f"⚠️ t{t['n']}: caminho inacessível ({e})")
            continue
        for key in t["coverage"]:
            ep = job["episodes"][key]
            if ep["state"] in ("skipped_future", "skipped_missing"):
                continue
            try:
                f = await asyncio.to_thread(
                    _find_episode_file, root, *_search_ref(job, key))
                ep["src"][t["role"]] = str(f)
            except Exception as e:  # noqa: BLE001
                jobs._event(job, "info",
                            f"⚠️ {key}: arquivo ({ROLE_LABEL[t['role']]}) não "
                            f"encontrado em t{t['n']}: {e}")
    for s, e in _active_refs(job):
        key = ep_key(s, e)
        ep = job["episodes"][key]
        if all(r in ep["src"] for r in ROLES):
            ep["state"] = "downloaded"
        elif job.get("force_continue"):
            # etapa forçada: o que não baixou foi PULADO pelo usuário, não falhou
            ep["state"] = "skipped_missing"
        else:
            faltou = [ROLE_LABEL[r] for r in ROLES if r not in ep["src"]]
            ep["state"] = "failed"
            ep["error"] = f"arquivo não baixado/encontrado: {', '.join(faltou)}"
    store.upsert_job(job)


def _find_episode_file(root, season: int, episode: int) -> Path:
    """Arquivo de vídeo do episódio dentro do diretório do torrent.

    Match por SxxEyy/1x02 no NOME do arquivo (substitui a heurística de filmes
    "maior arquivo da pasta", que não faz sentido em pack). Torrent de arquivo
    único sem referência no nome: é o próprio arquivo."""
    p = Path(root)
    if p.is_file():
        return p
    vids = [f for f in p.rglob("*")
            if f.suffix.lower() in jobs.VIDEO_EXTENSIONS
            and "sample" not in f.name.lower()]
    if not vids:
        raise RuntimeError(f"nenhum arquivo de vídeo em {p}")
    matches = [f for f in vids
               if (season, episode) in parse.parse_episode_refs(f.name)]
    if matches:
        # mais de um match (720p e 1080p no mesmo pack): fica o maior
        return max(matches, key=lambda f: f.stat().st_size)
    if len(vids) == 1:
        return vids[0]
    raise RuntimeError(
        f"nenhum dos {len(vids)} vídeos tem S{season:02d}E{episode:02d} no nome")


async def _after_download(job: dict):
    """Fim do download: entra no merge por episódio (caminho rápido)."""
    downloaded = [k for k, v in job["episodes"].items()
                  if v["state"] == "downloaded"]
    if not downloaded:
        # nada baixou: o relatório sai direto (o merge_all trataria igual, mas
        # sem episódios a mensagem de erro fica mais direta aqui)
        jobs._fail(job, "Nenhum episódio baixado com sucesso")
        return
    from services.series import merge_runner
    await merge_runner.merge_all(job)


# -------------------- integração com o registro de jobs --------------------

def resume(job: dict):
    """Chamado pelo jobs.resume_pending para jobs media_type=tv."""
    status = job["status"]
    if status == "searching":
        jobs._set(job, "error",
                  "Servidor reiniciado durante a busca — use ↻ para repetir")
    elif status == "downloading":
        jobs._spawn(job["id"], _resume_download(job))
    elif status == "merging":
        # os estados por episódio tornam a retomada idempotente: `done` fica,
        # `merging` interrompido volta a `downloaded` (os arquivos de origem
        # estão no disco) e o merge recomeça só do que falta
        for ep in job["episodes"].values():
            if ep["state"] == "merging":
                ep["state"] = "downloaded"
        jobs._spawn(job["id"], _resume_merge(job))
    # awaiting: gate persistido; segue esperando a decisão do usuário


async def _resume_merge(job: dict):
    try:
        from services.series import merge_runner
        await merge_runner.merge_all(job)
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001
        jobs._fail(job, f"{type(e).__name__}: {e}")


async def retry(old: dict) -> dict:
    """Recria o job de série com os mesmos parâmetros (chamado por jobs.retry)."""
    req = old.get("request") or {}
    return await create_series(
        old["tmdb_id"], old["language"],
        seasons=req.get("seasons"), episodes=req.get("episodes"),
        mode=old.get("mode", "auto"),
        destination_id=old.get("destination_id"),
        torrent_target_id=old.get("torrent_target_id"),
        convert=old.get("convert"))


def forget(job_id: str):
    """Limpa o estado em memória do job (cancel/remove)."""
    _pools.pop(job_id, None)
