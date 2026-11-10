"""Orquestrador de jobs de SÉRIE: TMDB TV -> Jackett -> qBittorrent.

Desacoplado do pipeline de filmes (services/jobs/) — daqui só usamos o
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
from services import candidates, jackett, jobs, stall_switch, store, tmdb, transcode
from services import selector
from services.series import parse, selector_tv, subs

ROLES = ("original", "dubbed")
ROLE_LABEL = {"original": "original", "dubbed": "dublado"}
KIND_LABEL = {"both": "original + dublado (merge)",
              "original": "só original", "dubbed": "só dublado"}


def _roles_phrase(roles) -> str:
    """"o original", "o dublado" ou "o original e o dublado" (mensagens)."""
    return " e ".join(f"o {ROLE_LABEL[r]}" for r in roles)


def roles_of(job: dict) -> tuple[str, ...]:
    """Papéis que este job baixa — o mesmo `kind` dos filmes. Job antigo
    (kind "series", de antes da opção existir) baixa os dois."""
    kind = job.get("kind")
    if kind == "original":
        return ("original",)
    if kind == "dubbed":
        return ("dubbed",)
    return ROLES


def single_role(job: dict) -> str | None:
    """O papel único de um job "só original"/"só dublado" (None = merge)."""
    roles = roles_of(job)
    return roles[0] if len(roles) == 1 else None
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
                        convert: dict | None = None,
                        skip_search: bool = False,
                        advanced_merge: dict | None = None,
                        kind: str = "both") -> dict:
    """Cria um job de série cobrindo temporadas inteiras e/ou episódios
    avulsos ({"3": [1, 5]}). `kind` como nos filmes: "both" baixa os dois
    lados de cada episódio e faz o merge; "original"/"dubbed" baixa um lado
    só e entrega o arquivo do episódio direto, sem merge."""
    from services import advanced_merge as adv_merge
    if kind not in jobs.KINDS:
        raise ValueError(f"kind inválido: {kind!r}")
    # um lado só: não há merge, então a política do merge avançado não se aplica
    advanced_merge = (adv_merge.validate_override(advanced_merge)
                      if kind == "both" else None)
    if skip_search and mode != "manual":
        raise ValueError("Pular a busca só faz sentido no modo manual")
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
    if dest is not None and dest.get("media", "movie") != "tv":
        raise ValueError(
            f"O destino '{dest['label']}' é da biblioteca de FILMES — séries "
            f"vão para um destino de séries (Configurações → Destinos)")
    if dest is None:
        dest = store.default_destination("tv")
    if dest is None:
        raise ValueError("Nenhum destino de SÉRIES cadastrado — crie um em "
                         "Configurações → Destinos (mídia: Séries)")
    target = store.get_torrent_target(torrent_target_id) if torrent_target_id else None
    if target is None:
        target = store.default_torrent_target()

    job = {
        "id": uuid.uuid4().hex[:10],
        "media_type": "tv",
        "tmdb_id": tmdb_id,
        "language": language,
        "mode": mode,
        "kind": kind,
        "skip_search": skip_search,
        "advanced_merge": advanced_merge,   # override da política (None = global)
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
        "progress": {},        # merge (M4+); download agregado vem dos torrents
        "output": None,
        # candidatos da busca: únicos por papel, com a posição de cada um no
        # ranking de cada episódio (manual/trocas — ver services/candidates.py)
        "search_queries": [],
        "search_candidates": [],
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
                f"Job de série criado ({' + '.join(scope)}; {KIND_LABEL[kind]}) "
                f"— destino: {dest['label']}{cinfo}")
    jobs._spawn(job["id"], _run(job))
    return jobs._public(job)


# -------------------- pipeline --------------------

async def _run(job: dict):
    try:
        await _prepare(job)
        if not job.get("skip_search"):
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
    # TODAS as temporadas do TMDB, incluindo a 0 ("Especiais") — a UI as lista
    # e o pedido tem que ser aceito. known_seasons (packs) fica SÓ com as > 0:
    # um torrent "série completa" não costuma trazer os especiais, e tratá-los
    # como cobertos criaria atribuições fantasma (episódio "baixado" que não
    # está no pack).
    all_seasons = {s["season"] for s in info["seasons"]
                   if s["season"] is not None and s["season"] >= 0}
    job["known_seasons"] = sorted(s for s in all_seasons if s > 0)

    req = job["request"]
    _check_requested_seasons(all_seasons, req)

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
        # sem a chave (quem lê usa `or []`): um [] aqui viraria "a série não
        # tem ordens alternativas" na tabela compartilhada (store,
        # media_episode_groups) e apagaria as que outro job já conhece
        job.pop("episode_groups", None)

    title = job["movie"]["original_title"]
    jobs._event(job, "info",
                f"Série: {title} ({job['movie']['year']}) — "
                f"{len(_active_refs(job))} episódio(s) a baixar")


def _check_requested_seasons(all_seasons: set[int], req: dict):
    """O pedido só pode referenciar temporadas que o TMDB conhece — incluindo
    a 0 (Especiais), que a UI lista e o usuário pode marcar."""
    bad = [s for s in req["seasons"] if s not in all_seasons]
    bad += [s for s in req["episodes"] if s not in all_seasons]
    if bad:
        raise ValueError("Temporada(s) inexistente(s) no TMDB: "
                         + ", ".join(f"S{s:02d}" for s in sorted(set(bad))))


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

    roles = roles_of(job)
    q_original = (season_queries(titles_original, portuguese=False)
                  if "original" in roles else [])
    # sem título localizado distinto, o lado dublado busca pelos mesmos nomes
    # (o marcador de idioma filtra depois)
    q_dubbed = (season_queries(loc_spellings or titles_original, portuguese=True)
                if "dubbed" in roles else [])

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
    partes = []
    if "original" in roles:
        partes.append(f"{len(pool_original)} resultados (original)")
    if "dubbed" in roles:
        partes.append(f"{len(pool_dubbed)} ({label})")
    jobs._event(job, "search", "Pool: " + ", ".join(partes))


def _rank_all(job: dict) -> dict[str, dict[tuple[int, int], list[dict]]]:
    """Ranking por episódio×papel a partir dos pools. Também grava os
    candidatos do job (únicos, com a posição em cada episódio) para a
    UI/manual/trocas."""
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
    candidates.reset(job)
    for role in roles_of(job):
        titles = titles_original if role == "original" else titles_dubbed
        ranked_all[role] = {}
        per_episode: dict[str, list[dict]] = {}
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
            per_episode[key] = [_slim(c) for c in ranked[:MAX_PER_EPISODE]]
        # o mesmo pack aparece no ranking de cada episódio que cobre: vira UM
        # candidato (no servidor, 2.185 cópias gravadas eram 124 torrents)
        candidates.set_series(job, role, per_episode)
    return ranked_all


def _slim(cand: dict) -> dict:
    return {"title": cand["title"], "tracker": cand.get("tracker"),
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
        if job["id"] not in _pools and not job.get("skip_search"):
            # pool perdido (restart/remap): refaz a busca antes de planejar
            await _search_series(job)
        jobs._set(job, "searching", "Avaliando candidatos por episódio...")
        ranked_all = _rank_all(job)

        # fase 2: episódios sem candidato em algum papel ganham buscas dedicadas
        missing = {role: [ref for ref in _active_refs(job)
                          if not ranked_all[role].get(ref)]
                   for role in roles_of(job)}
        if (any(missing.values()) and not job.get("_phase2_done")
                and not job.get("skip_search")):
            job["_phase2_done"] = True
            await _phase2_search(job, missing)
            ranked_all = _rank_all(job)

        plans = {role: selector_tv.build_plan(
            [_search_ref(job, ep_key(s, e)) for s, e in _active_refs(job)],
            {(_search_ref(job, ep_key(s, e))): ranked_all[role][(s, e)]
             for s, e in _active_refs(job)},
            known_seasons=set(job.get("known_seasons") or []))
            for role in roles_of(job)}
        _store_plan(job, plans)

        vazios = [r for r in roles_of(job)
                  if not any(t["role"] == r for t in job["torrents"])]
        if vazios and not job.get("skip_search"):
            # a busca não achou NADA para um papel (ou para o job): sem torrent
            # num papel, todo episódio é lacuna e "continuar com as lacunas"
            # não teria o que baixar — antes o job acabava em "Plano vazio".
            # Abre a escolha manual (que aceita magnet/link próprio por papel
            # e buscar de novo) SEM trocar o modo do job: um job auto que
            # depois acha tudo na nova busca segue sozinho, como os filmes
            detail = (f"Nenhum torrent encontrado para "
                      f"{_roles_phrase(vazios)} — cole um magnet/link ou "
                      f"tente buscar de novo")
            jobs._event(job, "search", f"⚠️ {detail}")
            _gate(job, "manual_pick", None, detail)
            return

    if job["mode"] == "manual" and not job.get("_manual_done"):
        _gate(job, "manual_pick", None, "Escolha os torrents (modo manual)")
        return

    gaps = _collect_gaps(job)
    if gaps and not job.get("_gaps_accepted"):
        _gate(job, "gaps_confirm", None,
              f"⚠️ {len(gaps)} episódio(s) sem torrent — decidir antes de baixar")
        return

    signals = _collect_signals(job)
    if signals and not job.get("_signals_accepted"):
        _gate(job, "incompatible_torrents", None,
              "⚠️ Torrent(s) com sinal de incompatibilidade — confirme antes de baixar")
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
    for role in roles_of(job):
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
    jobs._persist(job)


def _aired_ref(job: dict, search_ref: tuple[int, int]) -> tuple[int, int]:
    """Inverso de _search_ref: da referência de busca (ordem alternativa) para
    a chave de exibição usada em job["episodes"]."""
    om = job.get("order_map") or {}
    for key, alt in om.items():
        if tuple(alt) == tuple(search_ref):
            return parse_ep_key(key)
    return search_ref


def _preselected(by_torrent: dict[str, list[dict]]) -> list[dict]:
    """O plano atual como a escolha manual o pré-marca: cada torrent do plano
    com o id da linha que o representa em by_torrent e a cobertura de agora
    (a que os ARQUIVOS confirmaram, num torrent já enviado)."""
    out = [{"n": c["planned"]["n"], "role": role, "candidate_id": c["id"],
            "title": c["title"], "quality": c.get("quality"),
            "coverage": c["planned"]["coverage"],
            "state": c["planned"]["state"]}
           for role, lst in by_torrent.items() for c in lst if c["planned"]]
    return sorted(out, key=lambda p: p["n"])


def _sent(t: dict) -> bool:
    """O torrent já foi enviado ao qBittorrent por este job (baixa/baixou)."""
    return t["state"] != "pending"


def _collect_gaps(job: dict) -> list[dict]:
    """Episódios pedidos sem torrent em um (ou nos dois) papéis. TODOS passam
    pelo gate — a regra do produto é nunca decidir lacuna sozinho."""
    roles = roles_of(job)
    covered = {role: {ep for t in job["torrents"] if t["role"] == role
                      for ep in t["coverage"]} for role in roles}
    gaps = []
    for s, e in _active_refs(job):
        key = ep_key(s, e)
        roles_missing = [ROLE_LABEL[r] for r in roles if key not in covered[r]]
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


def _remaining_after_gaps(job: dict) -> list[str]:
    """Episódios que sobram para baixar se as lacunas forem aceitas (os
    cobertos em todos os papéis do job)."""
    gaps = {g["episode"] for g in _collect_gaps(job)}
    return [ep_key(s, e) for s, e in _active_refs(job)
            if ep_key(s, e) not in gaps]


def _plan_id(t: dict) -> str:
    """Id, na escolha manual, de um torrent do plano que não está entre os
    candidatos da busca (magnet próprio; candidato que uma busca refeita
    não trouxe de novo)."""
    return f"plan:{t['n']}"


def _plan_as_candidate(t: dict) -> dict:
    """Torrent do plano no formato de candidato (a cobertura do candidato é
    o RÓTULO; a do torrent é a lista de episódios)."""
    return {"id": _plan_id(t), "title": t["title"], "tracker": t.get("tracker"),
            "seeders": t.get("seeders"), "size": t.get("size"),
            "quality": t.get("quality"), "coverage": t.get("coverage_label"),
            "score": None, "tier": None, "magnet": t.get("magnet"),
            "link": t.get("link"), "infohash": t.get("infohash"),
            "files": t.get("files_count")}


def _planned_view(t: dict) -> dict:
    prog = t.get("progress")
    return {"n": t["n"], "state": t["state"], "coverage": list(t["coverage"]),
            "pct": prog.get("pct") if isinstance(prog, dict) else None}


def _title_matches(job: dict, title: str, shrunk=()) -> list[str]:
    """Episódios pedidos que o TÍTULO cobre, fora os que os ARQUIVOS de um
    torrent já enviado desmentiram (coverage_shrunk): devolvê-los a ele
    pelo "auto" recriaria a lacuna que o watchdog acabou de achar."""
    known = set(job.get("known_seasons") or [])
    cov = parse.parse_coverage(title)
    return [ep_key(s, e) for s, e in _active_refs(job)
            if ep_key(s, e) not in shrunk
            and cov.covers(*_search_ref(job, ep_key(s, e)), known)]


def _torrent_candidates(job: dict) -> dict[str, list[dict]]:
    """Candidatos ÚNICOS por papel (dedup por identidade), cada um com os
    episódios pedidos que o TÍTULO cobre — a visão invertida da escolha
    manual: marca-se o torrent e atribuem-se episódios a ele.

    Os torrents do PLANO (pendentes e os já enviados ao qBittorrent) vêm
    marcados em `planned` e no topo: a escolha abre com o plano atual
    pré-selecionado e a confirmação o SUBSTITUI. Caso real: o gate de
    lacunas aberto no meio do download levava a uma escolha vazia; o usuário
    marcava só o que faltava e o job juntava tudo, baixando em dobro."""
    out: dict[str, list[dict]] = {}
    for role in roles_of(job):
        plan: dict[str, dict] = {}
        for t in job["torrents"]:
            if t["role"] == role:
                plan.setdefault(parse.torrent_identity(t), t)
        seen: dict[str, dict] = {}

        def add(c: dict, ident: str):
            t = plan.get(ident)
            seen[ident] = {
                "id": c["id"], "title": c["title"],
                "tracker": c.get("tracker"), "seeders": c.get("seeders"),
                "size": c.get("size"), "quality": c.get("quality"),
                "coverage": c.get("coverage"), "score": c.get("score"),
                "tier": c.get("tier"),
                "matches": _title_matches(
                    job, c["title"], (t or {}).get("coverage_shrunk") or ()),
                "planned": _planned_view(t) if t else None,
            }
        for c in candidates.selectable(job, role):
            ident = parse.torrent_identity(c)
            if ident not in seen:
                add(c, ident)
        for ident, t in plan.items():
            if ident not in seen:
                add(_plan_as_candidate(t), ident)
        out[role] = sorted(seen.values(),
                           key=lambda c: (c["planned"] is None,
                                          (c["planned"] or {}).get("n", 0),
                                          -(c.get("tier") or 0),
                                          -len(c["matches"])))
    return out


def _alternatives_for(job: dict, torrent: dict) -> list[dict]:
    """Candidatos alternativos que cobrem TODOS os episódios deste torrent."""
    if not torrent["coverage"]:
        return []
    first = candidates.for_episode(job, torrent["role"], torrent["coverage"][0])
    out = []
    for c in first:
        if c["title"] == torrent["title"]:
            continue
        cov = parse.parse_coverage(c["title"])
        known = set(job.get("known_seasons") or [])
        if all(cov.covers(*_search_ref(job, k), known)
               for k in torrent["coverage"]):
            out.append(candidates.series_view(c))
    return out[:30]


# -------------------- gates --------------------

# Gates cujo conteúdo é DERIVÁVEL do estado persistido do job (episódios,
# torrents e candidatos da busca): o payload não é gravado — é calculado na
# hora em que a API entrega o job (awaiting_view). Gravar uma cópia de algo
# derivável só cria o risco de ela envelhecer (caso real: o gate de
# desalinhados ganhou os doadores concluídos, e um gate aberto antes da
# atualização não os teria). A escolha manual e os torrents incompatíveis
# mostram resultados de busca — que agora são candidatos persistidos do job,
# então também se recalculam (inclusive depois de um restart).
DERIVED_GATES = ("gaps_confirm", "alignment_review", "mismatched_pairs",
                 "manual_pick", "incompatible_torrents")


def gate_payload(job: dict, reason: str) -> dict | None:
    """Conteúdo de um gate derivado, a partir do estado atual do job."""
    if reason == "gaps_confirm":
        return {"missing": _collect_gaps(job),
                # aberto com download em curso (pack sem os arquivos esperados):
                # algum torrent já foi enviado ao qBittorrent
                "mid_download": any(t.get("hash") for t in job["torrents"]),
                # aceitar as lacunas não deixaria NENHUM episódio para baixar:
                # a UI não oferece "continuar" (o resolve também recusa)
                "nothing_left": not _remaining_after_gaps(job),
                "can_research": _can_research(job)}
    if reason == "alignment_review":
        # .get: o dict de uma listagem não carrega as EDLs (store.LIST_SECTIONS)
        return {"episodes": {k: v.get("edl") for k, v in sorted(job["episodes"].items())
                             if v["state"] == "review"}}
    if reason == "mismatched_pairs":
        from services.series import rematch
        return rematch.gate_payload(job)
    if reason == "manual_pick":
        # visão INVERTIDA (torrent -> episódios): candidatos únicos por papel
        # com os episódios pedidos que o TÍTULO de cada um cobre
        by = _torrent_candidates(job)
        return {"by_torrent": by,
                "requested": [ep_key(s, e) for s, e in _active_refs(job)],
                "preselected": _preselected(by),
                # papéis em que a busca não trouxe NENHUM candidato (a UI
                # explica e oferece o magnet/link próprio e a nova busca)
                "empty_roles": ([r for r in roles_of(job)
                                 if not candidates.selectable(job, r)]
                                if not job.get("skip_search") else []),
                "can_research": _can_research(job)}
    if reason == "incompatible_torrents":
        return {"torrents": _collect_signals(job),
                "episode_groups": job.get("episode_groups") or []}
    return None


def awaiting_view(job: dict) -> dict | None:
    """O `awaiting` como a UI o recebe: gate derivado com o payload calculado
    agora; os outros (drift) com o que foi gravado."""
    gate = job.get("awaiting")
    if not gate or gate.get("reason") not in DERIVED_GATES:
        return gate
    return {**gate, "payload": gate_payload(job, gate["reason"])}


def _gate(job: dict, reason: str, payload: dict | None, detail: str):
    """Pausa o job num gate explícito. A coroutine do pipeline RETORNA depois
    de chamar isto; o resolve() respawna a continuação. Gate derivado não
    leva payload (ver DERIVED_GATES)."""
    job["awaiting"] = {"reason": reason,
                       "payload": None if reason in DERIVED_GATES else payload}
    jobs._set(job, "awaiting", detail)


async def resolve(job_id: str, reason: str, decision: dict) -> dict | None:
    """Resolve o gate ativo (ou o force_continue durante o download).

    Decisões por gate:
    - manual_pick:           {torrents: [{candidate_id, role,
                              episodes: [ep_key,...] | "auto"}],
                              replace_plan: true}  (invertido; a seleção
                              SUBSTITUI o plano — ver _plan_manual_torrents)
                             ou {picks: {ep_key: {role: id}}}     (legado)
    - gaps_confirm:          {accept: bool} | {edit: true} (voltar ao manual)
    - incompatible_torrents: {action: "accept"}
                             {action: "replace", torrent_n, candidate_id}
                             {action: "remap", group_id}
    - mismatched_pairs:      {action: "auto", ignore: [ep_key,...]}
                             {action: "manual", pairs: {orig: dub}, ignore: [...]}
                             {action: "finish", ignore: [...]}
    - force_continue:        {} (aceito com o job em downloading)
    - edit_torrents:         {} (downloading) — reabre a escolha manual com o
                             plano atual marcado: remover, adicionar qualquer
                             resultado de qualquer papel, magnet próprio
    - remove_torrent:        {torrent_n} (downloading) — tira o torrent do
                             plano e do qBittorrent
    Os dois pedidos de download são atendidos pelo watchdog no próximo tick
    (_wait_downloads): ele é quem itera a lista de torrents, e mexer nela de
    fora no meio de um tick poderia reinserir um torrent recém-removido.
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

    if reason in ("edit_torrents", "remove_torrent"):
        if job["status"] != "downloading":
            raise ValueError("Só dá para editar os torrents durante o download "
                             "(na escolha manual e no aviso de lacunas, edite por lá)")
        if reason == "edit_torrents":
            job["_edit_torrents"] = True
            jobs._event(job, "chosen", "Usuário pediu para editar os torrents — "
                                       "abrindo a escolha (os downloads seguem)")
            return jobs._public(job)
        try:
            n = int(decision.get("torrent_n"))
        except (TypeError, ValueError):
            raise ValueError("torrent_n: informe o número do torrent")
        alvo = next((t for t in job["torrents"] if t["n"] == n), None)
        if alvo is None:
            raise ValueError(f"t{n} não está no plano")
        pedidos = job.setdefault("_remove_torrents", [])
        if n not in pedidos:
            pedidos.append(n)
        jobs._event(job, "chosen", f"Usuário pediu para remover t{n}: {alvo['title']}")
        return jobs._public(job)

    gate = job.get("awaiting")
    if job["status"] != "awaiting" or not gate or gate["reason"] != reason:
        raise ValueError(
            f"O job não está aguardando '{reason}'"
            + (f" (gate atual: {gate['reason']})" if gate else ""))

    # qualquer decisão inválida (ou falha ao aplicá-la — inclusive erro de rede
    # no remap) RESTAURA o gate: sem isso o job ficaria preso em awaiting sem
    # gate ativo, com todo resolve seguinte respondendo 409 para sempre
    job["awaiting"] = None
    try:
        if reason == "gaps_confirm":
            if decision.get("edit"):
                # voltar para a escolha manual e cobrir as lacunas na mão.
                # O job vira manual (verdadeiro daqui em diante) e o gate de
                # escolha reabre no próximo _plan_and_gate.
                job["mode"] = "manual"
                job.pop("_manual_done", None)
                jobs._event(job, "chosen",
                            "Usuário voltou para a escolha manual por causa "
                            "das lacunas")
            elif not decision.get("accept"):
                jobs._event(job, "chosen",
                            "Usuário desistiu por causa das lacunas")
                await jobs.cancel(job_id)
                cancelled = jobs._lookup(job_id)
                return jobs._public(cancelled) if cancelled else None
            else:
                if not _remaining_after_gaps(job):
                    # sem nenhum episódio coberto o job só acabaria em erro
                    # ("Plano vazio"/"Nenhum episódio baixado")
                    raise ValueError(
                        "Nenhum episódio tem torrent em todos os papéis — não "
                        "há o que baixar. Edite a seleção (dá para colar um "
                        "magnet/link próprio), busque de novo ou cancele o job")
                _apply_gaps(job)
                job["_gaps_accepted"] = True
                jobs._event(job, "chosen",
                            "Usuário aceitou continuar com as lacunas")
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
                raise ValueError(f"Ação inválida: {action!r}")
        elif reason == "manual_pick":
            await _resolve_manual(job, decision)
            job["_manual_done"] = True
        elif reason == "mismatched_pairs":
            from services.series import rematch
            action = decision.get("action")
            ignore = list(decision.get("ignore") or [])
            if action == "auto":
                jobs._event(job, "chosen",
                            "Usuário pediu rematch automático dos desalinhados")
                jobs._spawn(job["id"], rematch.run_auto(job, ignore))
            elif action == "manual":
                pairs = dict(decision.get("pairs") or {})
                dubs = list(pairs.values())
                if len(dubs) != len(set(dubs)):
                    raise ValueError("o mesmo dublado foi escolhido para dois "
                                     "episódios")
                rematch.apply_ignores(job, ignore)
                rematch.apply_pairs(job, pairs)   # valida contra o pool
                jobs._event(job, "chosen",
                            f"Usuário casou {len(pairs)} par(es) manualmente")
                jobs._spawn(job["id"], _resume_merge(job))
            elif action == "finish":
                rematch.apply_ignores(job, ignore)
                jobs._spawn(job["id"], rematch.finish_now(job))
            else:
                raise ValueError(f"Ação inválida: {action!r}")
            return jobs._public(job)
        elif reason == "alignment_review":
            _apply_review(job, decision)
            # episódios ainda sem decisão continuam em revisão: re-abre o gate
            remaining = [k for k, v in job["episodes"].items()
                         if v["state"] == "review"]
            if remaining:
                _gate(job, "alignment_review", None,
                      f"⚠️ {len(remaining)} episódio(s) ainda precisam de revisão")
                return jobs._public(job)
            jobs._spawn(job["id"], _resume_merge(job))
            return jobs._public(job)
        else:
            raise ValueError(f"Gate desconhecido: {reason!r}")
    except Exception:
        job["awaiting"] = gate  # gate continua de pé para uma nova decisão
        raise

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
    roles = roles_of(job)
    covered = {role: {ep for t in job["torrents"] if t["role"] == role
                      for ep in t["coverage"]} for role in roles}
    for s, e in _active_refs(job):
        key = ep_key(s, e)
        if any(key not in covered[r] for r in roles):
            job["episodes"][key]["state"] = "skipped_missing"
    # torrents que só existiam por esses episódios saem do plano — só os que
    # ainda NÃO foram enviados (pending); um que já baixa/baixou fica no plano
    # para o watchdog/limpeza continuarem enxergando-o
    for t in job["torrents"]:
        t["coverage"] = [k for k in t["coverage"]
                         if job["episodes"][k]["state"] not in
                         ("skipped_future", "skipped_missing")]
    job["torrents"] = [t for t in job["torrents"]
                       if t["coverage"] or t["state"] != "pending"]


def _apply_replace(job: dict, decision: dict):
    """Troca um torrent do plano por um candidato alternativo."""
    n = decision.get("torrent_n")
    cid = decision.get("candidate_id")
    entry = next((t for t in job["torrents"] if t["n"] == n), None)
    if entry is None:
        raise ValueError("Torrent não encontrado no plano")
    cand = candidates.find(job, cid, entry["role"])
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


async def _resolve_manual(job: dict, decision: dict):
    """Escolha manual vinda do resolve. No formato invertido, o qBittorrent
    vem PRIMEIRO: os torrents que saem do plano depois de enviados são
    removidos de lá antes de o plano novo valer — se o qBittorrent não
    responder, o resolve restaura o gate com o plano antigo intacto (nada
    de um plano que não baixa mais o torrent X enquanto o X segue baixando)."""
    if decision.get("torrents") is None:
        _apply_manual(job, decision)
        return
    plan, removed, msg = _plan_manual_torrents(job, decision)
    await _drop_sent(job, removed, plan)
    _commit_manual(job, plan, msg)


def _apply_manual(job: dict, decision: dict) -> list[dict]:
    """Aplica a escolha manual. Dois formatos:

    - INVERTIDO (a UI atual): {torrents: [{candidate_id, role,
      episodes: [ep_key,...] | "auto"}], replace_plan} — marca-se o torrent
      e diz-se quais episódios ele atende; "auto" usa o match pelo TÍTULO (e
      o match fino pelo ARQUIVO acontece de graça depois do download, no
      _resolve_episode_files). Regras em _plan_manual_torrents. Episódio
      atribuído que o título não indica é aceito (decisão explícita do
      usuário — a UI avisa antes).
    - por episódio (legado): {picks: {ep_key: {role: candidate_id}}}.

    Devolve os torrents que saíram do plano já tendo sido enviados ao
    qBittorrent (quem chama pelo resolve os remove de lá: _resolve_manual).
    """
    if decision.get("torrents") is not None:
        return _apply_manual_torrents(job, decision)
    picks = decision.get("picks") or {}
    if not picks:
        jobs._event(job, "chosen", "Modo manual: usuário manteve o plano automático")
        return []
    # agrupa escolhas por candidato (o mesmo pack escolhido para vários
    # episódios vira UM torrent)
    by_ident: dict[tuple[str, str], dict] = {}
    for key, roles in picks.items():
        if key not in job["episodes"]:
            raise ValueError(f"Episódio desconhecido: {key}")
        for role, cid in roles.items():
            if role not in roles_of(job):
                raise ValueError(f"Papel inválido para este job: {role}")
            cand = candidates.find(job, cid, role)
            if cand is not None and key not in cand["episodes"]:
                cand = None  # existe, mas não está no ranking deste episódio
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
    jobs._persist(job)
    return []


def _find_candidate(job: dict, role: str, candidate_id: str) -> dict:
    """Candidato de um papel pelo id (cada torrent é um candidato só, com a
    posição em cada episódio; o id antigo "S01E02:o3" ainda resolve)."""
    cand = candidates.find(job, candidate_id, role)
    if cand is not None:
        return cand
    raise ValueError(
        f"Candidato {candidate_id!r} não encontrado (a busca pode ter sido refeita)")


def _custom_candidate(sel: dict) -> dict:
    """Candidato a partir de um magnet/link informado pelo usuário (sem passar
    pelo Jackett). O título vem do campo `title`, do `dn=` do magnet ou do
    próprio link — é dele que sai a cobertura "auto" (SxxEyy/temporada)."""
    from urllib.parse import parse_qs, unquote_plus, urlparse
    url = (sel.get("magnet") or sel.get("link") or "").strip()
    if not url:
        raise ValueError("Informe um magnet: ou link de .torrent")
    if not (url.startswith("magnet:") or url.startswith(("http://", "https://"))):
        raise ValueError("Só aceito magnet: ou link http(s) de .torrent")
    title = (sel.get("title") or "").strip()
    if not title and url.startswith("magnet:"):
        q = parse_qs(urlparse(url).query)
        title = unquote_plus(q.get("dn", [""])[0])
    if not title:
        title = url.rsplit("/", 1)[-1].split("?", 1)[0] or "torrent manual"
    cov = parse.parse_coverage(title)
    return {
        "id": candidates.custom_id(url), "title": title,
        "tracker": "manual", "seeders": None, "size": 0,
        "quality": parse.pack_resolution_tier(title)[1],
        "coverage": cov.label(), "score": None, "tier": None,
        "magnet": url if url.startswith("magnet:") else None,
        "link": None if url.startswith("magnet:") else url,
        "infohash": None, "files": None,
    }


def _plan_torrent(job: dict, role: str, cid: str) -> dict:
    """Torrent do plano referenciado pela escolha manual ("plan:<n>"): o
    magnet próprio já planejado, que não é candidato da busca."""
    try:
        n = int(cid.split(":", 1)[1])
    except (IndexError, ValueError):
        raise ValueError(f"Torrent do plano inválido: {cid!r}")
    t = next((x for x in job["torrents"] if x["n"] == n and x["role"] == role),
             None)
    if t is None:
        raise ValueError(f"t{n} não está mais no plano ({ROLE_LABEL[role]})")
    return _plan_as_candidate(t)


def _new_entry(job: dict, n: int, role: str, cand: dict, eps) -> dict:
    return {
        "n": n, "role": role, "tag": f"dl-{job['id']}-t{n}",
        "title": cand["title"], "tracker": cand.get("tracker"),
        "seeders": cand.get("seeders"), "size": cand.get("size"),
        "quality": cand.get("quality"),
        "coverage_label": cand.get("coverage"),
        "magnet": cand.get("magnet"), "link": cand.get("link"),
        "infohash": cand.get("infohash"), "files_count": cand.get("files"),
        "coverage": sorted(eps), "state": "pending",
        "hash": None, "progress": None, "selected_files": None,
        "content_path": None,
    }


def _kept_entry(t: dict, eps) -> dict:
    """Torrent do plano MANTIDO pela escolha: a mesma entrada (n, tag, hash,
    estado — nada é reenviado ao qBittorrent), com a cobertura nova.

    Pack já enviado teve os arquivos escolhidos para a cobertura antiga. Se
    GANHA episódio, os arquivos dele podem estar com prioridade 0; se, ainda
    baixando, PERDE episódio para outro torrent, seguiria baixando-o em
    dobro. Nos dois casos selected_files volta a None e o watchdog refaz a
    seleção (religando/desligando arquivos); um pack que já tinha concluído
    e ganhou episódio volta a baixar."""
    new = dict(t, coverage=sorted(eps))
    grew = set(eps) - set(t["coverage"])
    shrank = set(t["coverage"]) - set(eps)
    if _sent(t) and t.get("selected_files") and (
            grew or (shrank and t["state"] == "downloading")):
        new["selected_files"] = None
        if t["state"] == "done":
            new.update(state="downloading", content_path=None)
    return new


def _plan_manual_torrents(job: dict, decision: dict
                          ) -> tuple[list[dict], list[dict], str]:
    """Formato invertido: torrents marcados -> episódios atribuídos. Cada item
    referencia um candidato da busca (candidate_id), um torrent do plano sem
    candidato ("plan:<n>") OU um magnet/link próprio (magnet/link + title
    opcional). PURO: devolve (plano novo, torrents enviados que saem, evento)
    sem mexer no job — _commit_manual aplica.

    Conflitos no mesmo papel: atribuição explícita vence a "auto" (entre
    explícitas, a última); entre "auto", vence a primeira da seleção — a UI
    manda o plano atual na frente. Nunca dois torrents do job cobrem o mesmo
    episódio no mesmo papel.

    replace_plan (a UI atual, que abre com o plano PRÉ-SELECIONADO): a
    seleção é o plano inteiro, em todos os papéis. Torrent mantido (mesma
    identidade: infohash/btih/link) continua sendo a mesma entrada; o que
    não foi marcado — ou ficou sem episódio — sai do plano, inclusive o que
    já baixa/baixou (o resolve o tira do qBittorrent); o novo entra pendente.

    Sem replace_plan (formato antigo, de uma aba aberta antes): só o papel
    com marca muda, e o torrent dele que já foi enviado fica, perdendo os
    episódios que a seleção assumiu — não dá para apagar dados baixados com
    base numa escolha que não mostrava o plano."""
    sels = decision.get("torrents") or []
    replace = bool(decision.get("replace_plan"))
    if not sels:
        if replace:
            raise ValueError(
                "Nenhum torrent marcado — marque ao menos um (ou cole um "
                "magnet/link próprio), busque de novo ou cancele o job")
        return (job["torrents"], [],
                "Modo manual: usuário manteve o plano automático")
    valid_keys = {ep_key(s, e) for s, e in _active_refs(job)}
    # (role, identidade) -> {cand, eps, explícitos}; a ordem é a da seleção
    chosen: dict[tuple[str, str], dict] = {}
    roles_touched: set[str] = set()
    n_custom = 0
    for sel in sels:
        role = sel.get("role")
        if role not in roles_of(job):
            raise ValueError(f"Papel inválido para este job: {role!r}")
        cid = sel.get("candidate_id")
        if sel.get("magnet") or sel.get("link"):
            cand = _custom_candidate(sel)
            n_custom += 1
        elif isinstance(cid, str) and cid.startswith("plan:"):
            cand = _plan_torrent(job, role, cid)
        else:
            cand = _find_candidate(job, role, cid)
        ident = parse.torrent_identity(cand)
        in_plan = next((t for t in job["torrents"] if t["role"] == role
                        and parse.torrent_identity(t) == ident), None)
        eps = sel.get("episodes", "auto")
        if eps == "auto":
            eps = _title_matches(job, cand["title"],
                                 (in_plan or {}).get("coverage_shrunk") or ())
            if not eps and cand["tracker"] == "manual":
                raise ValueError(
                    f"O título '{cand['title']}' não indica temporada/episódio "
                    f"— informe os episódios que este magnet cobre")
            explicit = False
        else:
            bad = [k for k in eps if k not in valid_keys]
            if bad:
                raise ValueError("Episódio(s) fora do pedido: " + ", ".join(bad))
            explicit = True
        roles_touched.add(role)
        entry = chosen.setdefault((role, ident), {"cand": cand, "eps": set(),
                                                  "explicit": set()})
        entry["eps"].update(eps)
        if explicit:
            entry["explicit"].update(eps)

    for role in roles_touched:
        taken: dict[str, tuple[str, str]] = {}
        for key, entry in chosen.items():
            if key[0] == role:
                for k in sorted(entry["explicit"]):
                    taken[k] = key            # explícito: o último vence
        for key, entry in chosen.items():
            if key[0] == role:
                # "auto": o primeiro que chega fica com o episódio
                entry["eps"] = {k for k in sorted(entry["eps"])
                                if taken.setdefault(k, key) == key}

    affected = set(roles_of(job)) if replace else roles_touched
    newly: dict[str, set[str]] = {r: set() for r in affected}
    for (role, _ident), entry in chosen.items():
        newly[role] |= entry["eps"]
    # n nunca repete o de um torrent que acabou de sair (a tag dl-<job>-t<n>
    # é como o watchdog acha o torrent no qBittorrent)
    next_n = max((t["n"] for t in job["torrents"]), default=-1) + 1
    plan: list[dict] = []
    removed: list[dict] = []
    kept: set[tuple[str, str]] = set()
    for t in job["torrents"]:
        if t["role"] not in affected:
            plan.append(t)
            continue
        key = (t["role"], parse.torrent_identity(t))
        entry = chosen.get(key)
        if entry is not None and entry["eps"] and key not in kept:
            kept.add(key)
            plan.append(_kept_entry(t, entry["eps"]))
            continue
        if replace or not _sent(t):
            if _sent(t):
                removed.append(t)
            continue
        # formato antigo: o que já baixa/baixou fica, sem os episódios que a
        # seleção assumiu
        plan.append(dict(t, coverage=[k for k in t["coverage"]
                                      if k not in newly[t["role"]]]))
    n_new = 0
    for key, entry in chosen.items():
        if not entry["eps"] or key in kept:
            continue
        plan.append(_new_entry(job, next_n + n_new, key[0], entry["cand"],
                               entry["eps"]))
        n_new += 1
    if not plan:
        raise ValueError(
            "A seleção não deixa nenhum torrent com episódio atribuído — "
            "marque ao menos um (ou cole um magnet/link próprio)")
    msg = (f"Modo manual: {len(sels)} torrent(s) escolhido(s) "
           f"({', '.join(sorted(roles_touched))})"
           + (f", {n_custom} magnet(s)/link(s) próprio(s)" if n_custom else "")
           + (f" — plano substituído: {len(kept)} mantido(s), {n_new} novo(s), "
              f"{len(removed)} enviado(s) removido(s)" if replace else "")
           + " — lacunas passam pelo gate normal")
    return plan, removed, msg


def _commit_manual(job: dict, plan: list[dict], msg: str):
    job["torrents"] = plan
    jobs._event(job, "chosen", msg)
    jobs._persist(job)


def _apply_manual_torrents(job: dict, decision: dict) -> list[dict]:
    """Calcula e aplica a escolha invertida (sem tocar no qBittorrent:
    devolve os enviados que saíram — o resolve usa _resolve_manual)."""
    plan, removed, msg = _plan_manual_torrents(job, decision)
    _commit_manual(job, plan, msg)
    return removed


async def _drop_sent(job: dict, removed: list[dict], plan: list[dict]):
    """Tira do qBittorrent os torrents que saíram do plano depois de enviados.

    Acha cada um pela tag dele (dl-<job>-t<n>), não pelo hash: um magnet
    ainda sem metadados já está no qBittorrent, mas o job ainda não leu o
    hash. Apaga COM os dados baixados — a mesma regra da troca de torrent
    (filme e série) e da limpeza do cancel: o torrent desmarcado é o que o
    usuário decidiu não usar, e deixá-lo lá (sem a tag, ninguém do job o
    limparia depois) é justamente o download em dobro do caso real; a UI
    confirma antes, avisando que os arquivos vão junto. O mesmo torrent
    servindo a OUTRO torrent do plano (pack DUAL nos dois papéis) ou a outro
    job (409 no add = tag do outro no mesmo torrent) perde só as tags que
    não valem mais (jobs.downloads._release_tagged)."""
    if not removed:
        return
    live = {t["tag"] for t in plan}
    for t in removed:
        try:
            await asyncio.wait_for(
                _release_entry(job, t, live, "saiu do plano"), timeout=30)
        except (asyncio.TimeoutError, *jobs._CONN_ERRORS) as e:
            reason = (f"{type(e).__name__}: {e}" if str(e)
                      else type(e).__name__)
            raise ValueError(
                f"O qBittorrent não respondeu ao remover t{t['n']} "
                f"({reason}) — a seleção não foi aplicada; tente de novo") from e


async def _release_entry(job: dict, t: dict, live: set[str], why: str):
    """Solta do qBittorrent o torrent da entrada `t` (achado pela tag dela),
    pela regra única de remoção: apagado com os dados só se nenhuma tag de
    `live` (outro torrent do plano) nem de outro job o usa. Erro de conexão
    sobe para quem chama."""
    for r in await jobs.downloads._release_tagged(jobs._qbit, job, t["tag"], live):
        if r["deleted"]:
            jobs._event(job, "qbit",
                        f"🗑️ t{t['n']} {why} — removido do qBittorrent com os "
                        f"dados baixados: {t['title']}")
        else:
            quem = jobs.downloads._why_kept(r, "outro torrent deste job")
            jobs._event(job, "qbit",
                        f"t{t['n']} {why} — o torrent fica no qBittorrent "
                        f"porque também serve a {quem}: {t['title']}")


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
        erro = await jobs.runtime.send_torrent(
            jobs._qbit, url, f"{shared_tag(job)},{t['tag']}", save_path)
        # mesmo sem ter ido, entra como baixando: o watchdog não o acha no
        # qBittorrent e reinsere depois da carência — um link lento não
        # derruba o job com os outros torrents na fila
        t["state"] = "downloading"
        if erro:
            jobs._event(job, "qbit",
                        f"⚠️ t{t['n']} não foi enviado ({erro}) — o "
                        f"acompanhamento tenta de novo em alguns minutos: {t['title']}")
        else:
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
    de série: seleção de arquivos em packs e force_continue.

    A troca automática segue a política global com o override de cada
    torrent (services/stall_switch), relidos a cada tick; o relógio de cada
    um vai para a tela do job (runtime.stall_publish)."""
    pending = lambda: [t for t in job["torrents"] if t["state"] == "downloading"]  # noqa: E731
    jobs.runtime.stall_forget(job)   # de um watchdog anterior (gate, restart)
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
        if job.get("_remove_torrents"):
            await _apply_removals(job)
        if job.pop("_edit_torrents", False):
            # para o watchdog (os torrents seguem baixando no qBittorrent) e
            # reabre a escolha com o plano atual marcado — o mesmo caminho do
            # "Editar seleção" das lacunas; ao confirmar, replace_plan remove o
            # que saiu, envia o que entrou e o watchdog recomeça
            jobs.runtime.stall_forget(job)
            job["mode"] = "manual"
            job.pop("_manual_done", None)
            _gate(job, "manual_pick", None,
                  "Editando os torrents — os downloads seguem no qBittorrent")
            return
        if not pending():
            break
        policy = stall_switch.get()
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
                    jobs.runtime.stall_forget(job, t["n"])
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
                if await _ensure_file_selection(job, t):
                    continue  # arquivos religados: o progresso deste tick é velho
                pct = info.get("progress", 0)
                t["progress"] = {
                    "pct": round(pct * 100, 1), "speed": info.get("dlspeed", 0),
                    "eta": info.get("eta"), "state": info.get("state"),
                    "seeds": info.get("num_seeds", 0), "name": info.get("name"),
                    "size": info.get("size", 0),
                    "downloaded": info.get("completed", 0),
                }
                if pct >= 1:
                    jobs.runtime.stall_forget(job, t["n"])
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
                limit_min = stall_switch.limit_minutes(
                    policy, state, t.get("stall_mode"), t.get("stall_minutes"))
                if state in ("stoppedDL", "pausedDL"):
                    st.update(since=time.monotonic(), warned=False)
                elif pct > st["pct"] + 1e-4:
                    st.update(pct=pct, since=time.monotonic(), warned=False)
                elif (limit_min is not None
                      and time.monotonic() - st["since"] > limit_min * 60):
                    if await _switch_stalled(job, t, limit_min):
                        st.update(pct=-1.0, since=time.monotonic(), warned=False)
                    elif not st["warned"]:
                        jobs._event(job, "qbit",
                                    f"⚠️ t{t['n']} sem progresso há {limit_min} "
                                    f"min e sem reserva — seguindo esperando")
                        st["warned"] = True
                jobs.runtime.stall_publish(
                    job, t["n"], st["since"], limit_min,
                    paused=state in ("stoppedDL", "pausedDL"),
                    no_backup=st["warned"])
            job["detail"] = _download_detail(job)
            # cobertura encolheu pelos arquivos reais e sobrou episódio sem
            # ninguém? pausa no gate de lacunas (os torrents seguem baixando
            # no qBittorrent — só o watchdog para até a decisão)
            if not job.get("_gaps_accepted") and _collect_gaps(job):
                gaps = _collect_gaps(job)
                jobs.runtime.stall_forget(job)
                _gate(job, "gaps_confirm", None,
                      f"⚠️ {len(gaps)} episódio(s) ficaram sem torrent (o pack "
                      f"não os contém) — decidir antes de continuar")
                return
            if time.monotonic() - last_persist >= config.PROGRESS_PERSIST_SECONDS:
                jobs._persist(job)
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

    jobs.runtime.stall_forget(job)
    await _resolve_episode_files(job)
    await _after_download(job)


def _download_detail(job: dict) -> str:
    done = sum(1 for t in job["torrents"] if t["state"] == "done")
    return f"Baixando torrents ({done}/{len(job['torrents'])} concluídos)..."


async def _apply_removals(job: dict):
    """Remove do plano (e do qBittorrent) os torrents pedidos pelo usuário.

    Mesma regra de remoção da edição de plano (_drop_sent): apagado com os
    dados só se nenhum outro torrent do plano nem de outro job usa o mesmo.
    Falha do qBittorrent não aplica nada (o plano fica como estava). Episódio
    que só o removido cobria vira lacuna — o gate de lacunas de sempre decide
    (editar, buscar de novo, seguir sem)."""
    ns = set(job.pop("_remove_torrents", None) or [])
    removed = [t for t in job["torrents"] if t["n"] in ns]
    if not removed:
        return
    plan = [t for t in job["torrents"] if t["n"] not in ns]
    try:
        await _drop_sent(job, removed, plan)
    except ValueError as e:
        jobs._event(job, "qbit", f"⚠️ {e}")
        return
    job["torrents"] = plan
    for t in removed:
        jobs.runtime.stall_forget(job, t["n"])
        jobs._event(job, "chosen", f"t{t['n']} removido do plano: {t['title']}")
    # lacunas que a remoção abriu são outra decisão
    job.pop("_gaps_accepted", None)
    jobs._persist(job)


def _apply_force_continue(job: dict):
    """Forçar a próxima etapa: torrents inacabados são abandonados (ficam no
    qBittorrent) e os episódios que dependiam só deles viram skipped_missing."""
    for t in job["torrents"]:
        if t["state"] == "downloading":
            t["state"] = "abandoned"
    jobs._event(job, "info",
                "⏭️ Próxima etapa forçada — torrents inacabados abandonados "
                "(continuam no qBittorrent)")


def _match_pack_files(job: dict, t: dict, files: list[dict]
                      ) -> tuple[list[dict], list[dict], set[str]]:
    """Cruza os arquivos REAIS do pack com a cobertura atribuída ao torrent.

    Retorna (manter, desmarcar, episódios com vídeo de fato presente). Um
    arquivo é mantido se o nome traz SxxEyy de algum episódio atribuído
    (vídeo + legendas dele); só arquivo de VÍDEO conta como "presente"."""
    wanted = {_search_ref(job, k): k for k in t["coverage"]}
    keep, drop = [], []
    found: set[str] = set()
    for f in files:
        full = f.get("name") or ""
        name = full.rsplit("/", 1)[-1]
        # SxxEyy no nome; legendas costumam vir em Subs/S01E01/2_English.srt
        # (a referência está na PASTA) — vale para arquivos de legenda
        refs = subs.refs_in_path(full) if Path(name).suffix.lower() in subs.TEXT_EXT \
            else parse.parse_episode_refs(name)
        hit = [wanted[r] for r in refs if r in wanted]
        if hit and Path(name).suffix.lower() in jobs.VIDEO_EXTENSIONS:
            found.update(hit)
        (keep if hit else drop).append(f)
    return keep, drop, found


async def _ensure_file_selection(job: dict, t: dict) -> bool:
    """Pack: baixa só os arquivos dos episódios atendidos por este torrent.

    Roda uma vez, quando os metadados chegam — e de novo quando a edição da
    escolha muda a cobertura de um pack já enviado (_kept_entry zera
    selected_files). Arquivo sem referência de episódio no nome fica FORA
    (não dá para saber o que é); se nenhum arquivo casar, mantém tudo (nome
    de arquivo fora do padrão — melhor baixar de mais do que de menos).

    Devolve True quando RELIGOU arquivo que uma seleção anterior desligou: o
    progresso lido neste tick é de antes disso (um pack concluído ainda
    marcaria 100%) e não vale."""
    if t["selected_files"] is not None or not t.get("hash"):
        return False
    cov = parse.parse_coverage(t["title"])
    if cov.kind in ("episode",):  # torrent de 1 episódio: nada a desmarcar
        t["selected_files"] = []
        return False
    files = await jobs._qbit.files(t["hash"])
    if not files:
        return False  # metadados ainda não chegaram; tenta no próximo tick
    keep, drop, found = _match_pack_files(job, t, files)
    # arquivo desligado (prioridade 0) por uma seleção anterior que agora
    # volta a ser preciso
    revive = [f["index"] for f in (keep or files) if f.get("priority") == 0]
    if revive:
        await jobs._qbit.file_prio(t["hash"], revive, 1)
    if not keep:
        t["selected_files"] = []
        jobs._event(job, "qbit",
                    f"⚠️ t{t['n']}: nenhum arquivo do pack casa com os episódios "
                    f"pedidos pelo nome — baixando o pack inteiro")
        return bool(revive)
    await jobs._qbit.file_prio(t["hash"], [f["index"] for f in drop], 0)
    t["selected_files"] = [f["name"] for f in keep]
    jobs._event(job, "qbit",
                f"t{t['n']}: pack com {len(files)} arquivo(s) — baixando "
                f"{len(keep)} ({len(drop)} desmarcados)")
    # MATCH FINO PELOS ARQUIVOS: o título prometeu a cobertura, os arquivos
    # dizem a verdade. Episódio atribuído que não tem vídeo no pack SAI da
    # cobertura agora — vira lacuna (gate) em vez de "arquivo não encontrado"
    # no fim do download. Ex.: "1ª Temporada Completa" com o ª corrompido lido
    # como série completa: só a S01 está lá.
    missing = [k for k in t["coverage"] if k not in found]
    if missing:
        t["coverage"] = [k for k in t["coverage"] if k in found]
        t["coverage_shrunk"] = sorted(set(t.get("coverage_shrunk") or []) | set(missing))
        jobs._event(job, "qbit",
                    f"⚠️ t{t['n']}: o pack NÃO contém {len(missing)} episódio(s) "
                    f"atribuído(s) pelo título ({', '.join(missing[:8])}"
                    f"{'…' if len(missing) > 8 else ''}) — removidos da cobertura")
        # lacunas novas passam pelo gate de novo, mesmo que o usuário já
        # tenha aceitado lacunas antes (são OUTRAS lacunas)
        job.pop("_gaps_accepted", None)
        jobs._persist(job)
    return bool(revive)


async def _readd(job: dict, t: dict):
    """Reinsere um torrent que sumiu do qBittorrent."""
    url = t.get("magnet") or t.get("link")
    if not url:
        jobs._event(job, "qbit",
                    f"⚠️ t{t['n']} sumiu e não tenho link para reinserir")
        return
    save_path = job.get("torrent_save_path") or config.QBIT_SAVE_PATH or None
    erro = await jobs.runtime.send_torrent(
        jobs._qbit, url, f"{shared_tag(job)},{t['tag']}", save_path)
    if erro:
        # link da busca lento (Jackett/tracker): não é o qBittorrent fora do
        # ar — tenta de novo na próxima carência
        jobs._event(job, "qbit", f"⚠️ t{t['n']} ainda não foi para o "
                                 f"qBittorrent ({erro}) — tento de novo depois: {t['title']}")
        return
    t["selected_files"] = None  # reseleciona os arquivos quando reaparecer
    jobs._event(job, "qbit", f"🔁 t{t['n']} reinserido: {t['title']}")


def _find_replacement(job: dict, t: dict) -> dict | None:
    """Próximo candidato do MESMO papel que cubra os MESMOS episódios do
    torrent `t` e ainda não esteja em uso no plano. None = sem reserva."""
    first = (candidates.for_episode(job, t["role"], t["coverage"][0])
             if t["coverage"] else None)
    if not first:
        return None
    known = set(job.get("known_seasons") or [])
    used = {parse.torrent_identity(x) for x in job["torrents"]}
    for c in first:
        if parse.torrent_identity(c) in used:
            continue
        cov = parse.parse_coverage(c["title"])
        if all(cov.covers(*_search_ref(job, k), known) for k in t["coverage"]):
            return c
    return None


async def _replace_entry(job: dict, t: dict, nxt: dict, reason: str):
    """Substitui o torrent do plano por `nxt`: solta o antigo do qBittorrent,
    atualiza a entrada in place (mesma tag — o watchdog zera o relógio quando
    o hash muda) e adiciona o novo.

    O antigo sai pela regra única de remoção (_release_entry), não por hash:
    apagado com os dados só se não serve a mais ninguém. Pack DUAL escolhido
    para original e dublado é UM torrent com as tags dos dois — trocar o
    dublado apagava do disco o pack que o original usava; agora ele perde só
    a tag do dublado (idem torrent que outro job também usa). Sai ANTES do
    novo entrar — com a mesma tag, o novo seria achado junto. Sem resposta do
    qBittorrent ao soltar o antigo, o erro sobe antes de a entrada mudar: o
    watchdog tenta de novo no próximo tick, a troca manual avisa que não
    aplicou."""
    live = {x["tag"] for x in job["torrents"] if x is not t}
    await _release_entry(job, t, live, "trocado")
    t.update(title=nxt["title"], tracker=nxt.get("tracker"),
             seeders=nxt.get("seeders"), size=nxt.get("size"),
             quality=nxt.get("quality"), magnet=nxt.get("magnet"),
             link=nxt.get("link"), infohash=nxt.get("infohash"),
             coverage_label=nxt.get("coverage"),
             hash=None, selected_files=None, progress=None)
    jobs._event(job, "qbit", f"{reason}: {nxt['title']}")
    save_path = job.get("torrent_save_path") or config.QBIT_SAVE_PATH or None
    try:
        await jobs._qbit.add(nxt.get("magnet") or nxt["link"],
                             f"{shared_tag(job)},{t['tag']}", save_path)
    except jobs._CONN_ERRORS as e:
        # o antigo já saiu: a troca vale. O watchdog não acha a tag e
        # reinsere o novo depois da janela de metadados (_readd)
        jobs._event(job, "qbit",
                    f"⚠️ t{t['n']}: o qBittorrent não aceitou o novo agora "
                    f"({type(e).__name__}) — o watchdog o reinsere se não "
                    f"aparecer")
    jobs._persist(job)


async def _switch_stalled(job: dict, t: dict, limit_min: int) -> bool:
    """Watchdog: troca um torrent travado por um candidato que cubra os MESMOS
    episódios. Sem reserva compatível, avisa e segue esperando."""
    nxt = _find_replacement(job, t)
    if nxt is None:
        return False
    await _replace_entry(job, t, nxt,
                         f"⏳ t{t['n']} travado há {limit_min} min — trocado por")
    return True


async def switch_torrent(job_id: str, torrent_n: int,
                         candidate_id: str | None = None) -> dict:
    """Troca manual de um torrent do plano durante o download (como nos
    filmes): sem candidate_id, "tentar próximo(s)" — a próxima reserva que
    cobre os MESMOS episódios; com candidate_id, o candidato escolhido.

    Candidato cujo TÍTULO não cobre todos os episódios do torrent é recusado
    com a lista do que falta — para atribuição diferente, use a edição manual
    (gate de lacunas → editar seleção)."""
    job = jobs._jobs.get(job_id)
    if not job or job.get("media_type") != "tv":
        raise ValueError("Job de série não encontrado")
    if job["status"] != "downloading":
        raise ValueError("Só dá para trocar torrent durante o download")
    t = next((x for x in job["torrents"] if x["n"] == torrent_n), None)
    if t is None:
        raise ValueError(f"Torrent t{torrent_n} não existe neste job")
    if t["state"] not in ("downloading", "pending"):
        raise ValueError(f"t{torrent_n} não está baixando ({t['state']})")

    if candidate_id is None:
        nxt = _find_replacement(job, t)
        if nxt is None:
            raise ValueError(
                "Sem candidato reserva que cubra os mesmos episódios "
                f"({', '.join(t['coverage'])})")
    else:
        nxt = _find_candidate(job, t["role"], candidate_id)
        if parse.torrent_identity(nxt) in {
                parse.torrent_identity(x) for x in job["torrents"]}:
            raise ValueError("Este torrent já está no plano")
        known = set(job.get("known_seasons") or [])
        cov = parse.parse_coverage(nxt["title"])
        missing = [k for k in t["coverage"]
                   if not cov.covers(*_search_ref(job, k), known)]
        if missing:
            raise ValueError(
                "O título do candidato não cobre: " + ", ".join(missing)
                + " — use a edição manual para atribuir episódios diferentes")
    try:
        await _replace_entry(job, t, nxt, f"🔁 Troca manual em t{t['n']}")
    except jobs._CONN_ERRORS as e:
        reason = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
        raise ValueError(f"O qBittorrent não respondeu ({reason}) — a troca "
                         f"de t{t['n']} não foi aplicada; tente de novo") from e
    return jobs._public(job)


def torrent_alternatives(job_id: str, torrent_n: int) -> list[dict]:
    """Candidatos que cobrem os mesmos episódios de um torrent do plano — a
    lista do "Escolher outro…" da UI."""
    job = jobs._jobs.get(job_id)
    if not job or job.get("media_type") != "tv":
        raise ValueError("Job de série não encontrado")
    t = next((x for x in job["torrents"] if x["n"] == torrent_n), None)
    if t is None:
        raise ValueError(f"Torrent t{torrent_n} não existe neste job")
    # o torrent EM USO vem no topo (a UI o marca e não troca por ele): sem
    # ele a lista não dizia qual era o atual. _alternatives_for segue sem —
    # a troca automática usa a mesma lista e não pode "trocar" por ele mesmo
    atual = next((candidates.series_view(c) for c in
                  candidates.for_episode(job, t["role"], t["coverage"][0])
                  if c["title"] == t["title"]), None) if t["coverage"] else None
    return ([atual] if atual else []) + _alternatives_for(job, t)


def _can_research(job: dict) -> bool:
    """Dá para refazer a busca do zero? Só antes de qualquer torrent ir para
    o qBittorrent (depois disso o plano tem downloads a preservar — o
    caminho é editar a seleção) e se o job não pulou a busca."""
    return (not job.get("skip_search")
            and not any(_sent(t) for t in job["torrents"]))


async def research(job_id: str) -> dict:
    """"Buscar de novo" na escolha manual / no aviso de lacunas (ex.: a busca
    não achou nada e o indexador estava fora, ou o release saiu agora).
    Descarta o plano e o pool e reentra no _plan_and_gate, que refaz a busca
    quando o pool não existe — o mesmo caminho de um restart."""
    job = jobs._jobs.get(job_id)
    if not job or job.get("media_type") != "tv":
        raise ValueError("Job de série não encontrado")
    gate = job.get("awaiting")
    if (job["status"] != "awaiting" or not gate
            or gate["reason"] not in ("manual_pick", "gaps_confirm")):
        raise ValueError("Só dá para buscar de novo na escolha dos torrents "
                         "ou no aviso de lacunas")
    if job.get("skip_search"):
        raise ValueError("Este job pulou a busca de torrents — cole o "
                         "magnet/link na escolha")
    if not _can_research(job):
        raise ValueError("Já há torrent deste job no qBittorrent — use "
                         "\"Editar seleção\" para trocar o plano")
    job["awaiting"] = None
    job["torrents"] = []
    _pools.pop(job_id, None)
    # decisões tomadas sobre o plano antigo não valem para o novo
    for flag in ("_phase2_done", "_gaps_accepted", "_signals_accepted",
                 "_manual_done"):
        job.pop(flag, None)
    jobs._event(job, "chosen", "Usuário pediu uma nova busca de torrents")
    jobs._set(job, "searching", "Buscando de novo no Jackett...")
    jobs._spawn(job_id, _continue_after_gate(job))
    return jobs._public(job)


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
                # legendas externas do episódio (srt/ass/vtt) — entram no
                # MKV final, com os tempos tratados no merge
                ext = await asyncio.to_thread(
                    subs.find_for_episode, root, *_search_ref(job, key), str(f))
                ep.setdefault("subs", {})[t["role"]] = [str(x) for x in ext]
                if ext:
                    jobs._event(job, "info",
                                f"{key}: {len(ext)} legenda(s) externa(s) no "
                                f"torrent {ROLE_LABEL[t['role']]}")
            except Exception as e:  # noqa: BLE001
                jobs._event(job, "info",
                            f"⚠️ {key}: arquivo ({ROLE_LABEL[t['role']]}) não "
                            f"encontrado em t{t['n']}: {e}")
    for s, e in _active_refs(job):
        key = ep_key(s, e)
        ep = job["episodes"][key]
        if all(r in ep["src"] for r in roles_of(job)):
            ep["state"] = "downloaded"
        elif job.get("force_continue"):
            # etapa forçada: o que não baixou foi PULADO pelo usuário, não falhou
            ep["state"] = "skipped_missing"
        else:
            faltou = [ROLE_LABEL[r] for r in roles_of(job) if r not in ep["src"]]
            ep["state"] = "failed"
            ep["error"] = f"arquivo não baixado/encontrado: {', '.join(faltou)}"
    jobs._persist(job)


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
    if job.get("recompress"):
        # recompressão em lote: os originais estão no disco — item que estava
        # convertendo volta para a fila e o laço recomeça só do que falta
        from services.series import recompress
        for ep in job["episodes"].values():
            if ep["state"] == "merging":
                ep["state"] = "downloaded"
        jobs._spawn(job["id"], recompress._run(job))
        return
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
    if old.get("recompress"):
        from services.series import recompress
        r = old["recompress"]
        return await recompress.create_recompress_batch(
            old.get("destination_id"), r["folder"], r["rels"],
            old["convert"], r.get("replace", True), old.get("tmdb_id"))
    if old.get("manual_rows"):
        # merge manual: os arquivos estão no disco, então repetir é refazer o
        # mesmo mapeamento — não uma busca nova
        from services.series import manual
        return await manual.create(
            old["tmdb_id"], old["language"], old["manual_rows"],
            old.get("destination_id"), old.get("convert"))
    req = old.get("request") or {}
    return await create_series(
        old["tmdb_id"], old["language"],
        seasons=req.get("seasons"), episodes=req.get("episodes"),
        mode=old.get("mode", "auto"),
        destination_id=old.get("destination_id"),
        torrent_target_id=old.get("torrent_target_id"),
        convert=old.get("convert"),
        advanced_merge=old.get("advanced_merge"),
        # job anterior à opção carrega kind "series": os dois lados
        kind=old.get("kind") if old.get("kind") in jobs.KINDS else "both")


def forget(job_id: str):
    """Limpa o estado em memória do job (cancel/remove)."""
    _pools.pop(job_id, None)
