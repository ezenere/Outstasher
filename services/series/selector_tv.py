"""Seleção de torrents para SÉRIES: ranking por episódio + plano de cobertura.

Reusa a escada de qualidade e os marcadores de dublagem do selector de filmes
(services/selector.py); o que muda é a unidade de decisão: cada EPISÓDIO
pedido precisa de um torrent que o cubra (avulso ou pack), nos dois idiomas.

Política de packs (decidida com o usuário): qualidade primeiro — a escada
ordena; em qualidade equivalente vence o torrent que cobre MAIS episódios
pedidos (menos torrents, menos buscas); o tamanho por episódio desempata.
"""
import config
from services import selector
from services.series import parse

# trace com o mesmo shape do de filmes (CandidatesTable renderiza sem mudança);
# "coverage" é a coluna extra de séries
MAX_TRACE = selector.MAX_TRACE


def rank_for_episode(results: list[dict], mode: str, series_title,
                     year: str, season: int, episode: int,
                     language: str | None = None,
                     require_language: bool = False,
                     dubbed_title: str | None = None,
                     requested: list[tuple[int, int]] | None = None,
                     known_seasons: set[int] | None = None,
                     ) -> tuple[list[dict], list[dict]]:
    """(viáveis ordenados, trace) para UM episódio pedido.

    mode: "video" (original) ou "audio" (dublado) — mesma semântica de filmes.
    series_title: título ou lista de títulos alternativos (original/inglês).
    requested: todos os episódios pedidos no job — packs que cobrem mais deles
    ganham o desempate dentro do mesmo tier.
    known_seasons: temporadas existentes (TMDB), para "série completa" saber o
    que cobre.
    """
    titles = ([series_title] if isinstance(series_title, str)
              else list(series_title))
    requested = requested or [(season, episode)]
    pairs: list[tuple[dict, dict]] = []
    for r in results:
        cov = parse.parse_coverage(r["title"])
        # título de série SEM os tokens de episódio, para o matching de
        # palavras não tropeçar em "S01E02"/"complete"
        bare = parse.strip_episode_tokens(r["title"])
        tier, quality = parse.pack_resolution_tier(r["title"])
        strong = (mode == "audio" and language
                  and selector.marker_strength(r["title"], language,
                                               dubbed_title) == 2)
        # nº de episódios PEDIDOS que este torrent cobre (o desempate de pack)
        cover_count = sum(1 for s, e in requested
                          if cov.covers(s, e, known_seasons))
        cand = {
            "title": r["title"],
            "tracker": r.get("tracker"),
            "seeders": r["seeders"],
            "size": r["size"],
            "edition": None,  # corte de filme não se aplica; coluna fica vazia
            "coverage": cov.label(),
            "quality": quality,
            "score": None,
            "rejected": None,
            "chosen": False,
            "year_match": False,
        }
        if not (r.get("magnet") or r.get("link")):
            cand["rejected"] = "sem magnet/link"
        elif not any(selector.matches_title(bare, t) for t in titles):
            cand["rejected"] = "título não bate"
        elif not cov.covers(season, episode, known_seasons):
            cand["rejected"] = f"não cobre S{season:02d}E{episode:02d}"
        elif require_language and language and not selector.has_language_marker(
                r["title"], language, dubbed_title):
            cand["rejected"] = ("sem marcador de idioma "
                                f"({config.LANGUAGES[language]['label']})")
        elif mode == "audio" and language and selector.is_subs_only(
                r["title"], language, dubbed_title):
            cand["rejected"] = "só legendado (áudio original, sem dublagem)"
        elif r["seeders"] <= 0:
            cand["rejected"] = "sem seeders"
        elif tier <= selector.MIN_TIER:
            cand["rejected"] = "qualidade muito baixa (CAM/TS)"
        else:
            cand["score"] = round(
                selector.score(r, mode, language, dubbed_title), 1)
        # tamanho POR EPISÓDIO: comparar o tamanho bruto de um pack com o de um
        # avulso favoreceria o pack só por ser maior
        total_eps = _total_episodes(cov, known_seasons)
        size_per_ep = (r["size"] or 0) / max(1, total_eps)
        cand["_sort"] = (
            bool(strong), tier, cover_count, size_per_ep,
            selector._audio_score(r["title"]),
            selector._seeders_score(r["seeders"]),
        )
        pairs.append((cand, r))

    viable = [(c, r) for c, r in pairs if c["rejected"] is None]
    viable.sort(key=lambda p: p[0]["_sort"], reverse=True)
    ranked = []
    for c, r in viable:
        item = dict(r)
        item["score"] = c["score"]
        item["quality"] = c["quality"]
        item["coverage"] = c["coverage"]
        item["tier"] = c["_sort"][1]
        item["cover_count"] = c["_sort"][2]
        ranked.append(item)

    trace = sorted(
        (c for c, _ in pairs),
        key=lambda c: (not c["chosen"], c["rejected"] is not None,
                       tuple(-float(x) for x in c["_sort"])))
    for c, _ in pairs:  # _sort é interno: não vaza para o trace/JSON
        c.pop("_sort", None)
    return ranked, trace[:MAX_TRACE]


def _total_episodes(cov: parse.Coverage,
                    known_seasons: set[int] | None) -> int:
    """Estimativa de episódios que o torrent contém (para tamanho/episódio).

    Sem contagem real por temporada aqui, usa ~12 por temporada — só um
    NORMALIZADOR de tamanho, não precisa ser exato."""
    if cov.kind in ("episode", "multi_episode"):
        return len(cov.episodes or []) or 1
    if cov.kind == "season_pack":
        return max(1, len(cov.seasons)) * 12
    if cov.kind == "complete":
        return max(1, len(known_seasons or {1})) * 12
    return 1


def build_plan(requested: list[tuple[int, int]],
               ranked_by_episode: dict[tuple[int, int], list[dict]],
               known_seasons: set[int] | None = None) -> dict:
    """Cobertura dos episódios pedidos com o MENOR conjunto de torrents que não
    sacrifica qualidade.

    Guloso por episódio (na ordem pedida): o melhor candidato do episódio
    define o TIER alvo; entre os candidatos daquele tier, um torrent JÁ
    escolhido para outro episódio vence (não baixar dois packs equivalentes,
    nem um avulso redundante com um pack já escolhido).

    Retorna {"assignments": {(s,e): candidato}, "gaps": [(s,e)...],
             "torrents": [candidatos únicos com "coverage_assigned"]}.
    """
    chosen: dict[str, dict] = {}  # identity -> candidato
    assignments: dict[tuple[int, int], dict] = {}
    gaps: list[tuple[int, int]] = []

    for s, e in requested:
        ranked = ranked_by_episode.get((s, e)) or []
        if not ranked:
            gaps.append((s, e))
            continue
        target_tier = ranked[0].get("tier", 0)
        pick = None
        # 1º passe: alguém do tier alvo que já esteja escolhido?
        for cand in ranked:
            if cand.get("tier", 0) != target_tier:
                break  # lista ordenada: saiu do tier alvo, para
            if parse.torrent_identity(cand) in chosen:
                pick = cand
                break
        if pick is None:
            pick = ranked[0]
        ident = parse.torrent_identity(pick)
        chosen.setdefault(ident, pick)
        assignments[(s, e)] = chosen[ident]

    torrents = []
    for ident, cand in chosen.items():
        covered = [ep for ep, a in assignments.items()
                   if parse.torrent_identity(a) == ident]
        item = dict(cand)
        item["coverage_assigned"] = sorted(covered)
        torrents.append(item)
    return {"assignments": assignments, "gaps": gaps, "torrents": torrents}
