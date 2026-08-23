"""Busca no Jackett e escolha dos candidatos (filmes).

Monta as queries (grafias alternativas, ano, idioma), deduplica por infohash,
pontua com o `selector` e guarda no job o rastro de cada decisão.
"""

import asyncio
import re
from urllib.parse import parse_qs, unquote_plus, urlparse

import config
from services import jackett, selector, store, tmdb
from services.jobs.runtime import (
    MAX_SELECTABLE, _event, _needed_torrents, _set)


def custom_candidate(url: str, title: str = "") -> dict:
    """Candidato a partir de um magnet/link informado pelo usuário (sem passar
    pelo Jackett), no mesmo formato dos candidatos da busca.

    O título vem do campo informado, do `dn=` do magnet ou do próprio link —
    é dele que saem o corte (edition) e a qualidade mostrados na UI.
    """
    url = (url or "").strip()
    if not url:
        raise ValueError("Informe um magnet: ou link de .torrent")
    if not (url.startswith("magnet:") or url.startswith(("http://", "https://"))):
        raise ValueError("Só aceito magnet: ou link http(s) de .torrent")
    title = (title or "").strip()
    if not title and url.startswith("magnet:"):
        title = unquote_plus(parse_qs(urlparse(url).query).get("dn", [""])[0])
    if not title:
        title = url.rsplit("/", 1)[-1].split("?", 1)[0] or "torrent manual"
    return {
        "id": f"custom:{abs(hash(url)) % 10 ** 8}", "title": title,
        "tracker": "manual", "seeders": None, "size": 0,
        "edition": selector.edition_of(title), "score": None,
        "quality": selector.quality_tier(title)[1],
        "magnet": url if url.startswith("magnet:") else None,
        "link": None if url.startswith("magnet:") else url,
    }


def _slim(cand: dict, cid: str) -> dict:
    return {"id": cid, "title": cand["title"], "tracker": cand.get("tracker"),
            "seeders": cand["seeders"], "size": cand["size"],
            "edition": cand.get("edition"), "score": cand["score"],
            "magnet": cand.get("magnet"), "link": cand.get("link")}


def _torrent_identity(r: dict) -> str | None:
    """Identidade ESTÁVEL do torrent para dedup, ignorando tokens voláteis.

    - magnet: usa o hash btih (canônico; ignora trackers/dn do magnet).
    - link do Jackett (/dl/...): a URL inteira NÃO serve — o parâmetro `path`
      é um token efêmero, diferente a cada busca do MESMO release. Usamos o
      parâmetro `file` (nome do release), que é estável. Sem `file`, cai para o
      link sem a query string.
    Retorna None quando não há magnet nem link (o rank rejeita depois)."""
    mag = r.get("magnet")
    if mag:
        m = re.search(r"btih:([0-9a-zA-Z]+)", mag)
        return f"hash:{m.group(1).lower()}" if m else f"magnet:{mag}"
    link = r.get("link")
    if link:
        m = re.search(r"[?&]file=([^&]+)", link)
        if m:
            return f"file:{unquote_plus(m.group(1)).lower()}"
        return f"link:{link.split('?', 1)[0]}"  # sem query volátil
    return None


def _dedup_results(results: list[dict]) -> list[dict]:
    """Remove torrents repetidos mantendo a 1ª ocorrência.

    As buscas adicionais (variantes de grafia × regras × indexers) retornam
    muito o MESMO release, então o pool combinado vem cheio de duplicatas. Sem
    isso, o rank processa/mostra N cópias de cada torrent.

    Chaveia por (identidade estável × PROVIDER): o mesmo torrent vindo de
    trackers diferentes é MANTIDO, porque um tracker às vezes nomeia melhor que
    o outro (mais info no título) — deixamos os dois concorrerem no rank. Só
    colapsa duplicatas do mesmo torrent NO MESMO tracker. Sem identidade, passa
    direto (o rank rejeita depois)."""
    seen = set()
    out = []
    for r in results:
        ident = _torrent_identity(r)
        if ident is not None:
            provider = r.get("tracker_id") or r.get("tracker")
            key = (ident, provider)
            if key in seen:
                continue
            seen.add(key)
        out.append(r)
    return out


def _extra_searches(spellings: list[str], year: str, lang: str) -> list[dict]:
    """Buscas extras direcionadas conforme as regras (idioma x variante x indexers).

    Cruza cada GRAFIA do título localizado (o título base + as variantes de
    caractere especial de title_variants: "&"->"e", pontuação removida...) com
    cada REGRA ESPECIAL (tirar o ano, romano->arábico). Ou seja, para cada
    grafia geramos também a versão sem ano e a versão com o numeral em arábico,
    combinando as duas coisas (ex.: "Franquia Exemplo IX" -> "Franquia
    Exemplo 9", "Franquia Exemplo 9" sem ano, etc.).

    Cada combinação só entra se produzir uma query DIFERENTE das buscas normais
    (as grafias já buscadas com ano) e se houver indexers configurados para
    aquela regra no idioma. Retorna uma lista de {query, indexer, variant} —
    uma entrada por indexer.
    """
    rules = store.get_extra_search_rules().get(lang) or {}
    if not spellings or not rules:
        return []

    out: list[dict] = []
    # as grafias já são buscadas COM ano nas buscas normais — não repetir
    seen_queries = {f"{s} {year}".strip().lower() for s in spellings}
    for spelling in spellings:
        arabic = selector._roman_to_arabic(spelling)
        has_roman = arabic != spelling
        # cada regra -> a query que ela gera para ESTA grafia (ou None se n/a)
        variant_query = {
            "no_year": spelling if year else None,
            "roman": f"{arabic} {year}".strip() if has_roman else None,
            "roman_no_year": arabic if (has_roman and year) else None,
        }
        for variant, query in variant_query.items():
            if not query:
                continue
            indexers = rules.get(variant) or []
            if not indexers:
                continue
            if query.lower() in seen_queries:
                continue  # não repete uma query que outra combinação já cobriu
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


async def _load_movie(job: dict) -> dict:
    """Metadados do TMDB no job (título/ano/pôster). Separado da busca porque
    o modo "pular busca" precisa deles sem consultar o indexador."""
    lang = job["language"]
    label = config.LANGUAGES[lang]["label"]
    movie = await tmdb.details(job["tmdb_id"], lang)
    job["movie"] = movie
    _event(job, "info", f"Filme: {movie['original_title']} ({movie['year']}) "
                        f"— título em {label}: {movie['localized_title']}")
    return movie


async def _search(job: dict):
    """Busca no Jackett e preenche job["search"] com os candidatos viáveis."""
    lang = job["language"]
    label = config.LANGUAGES[lang]["label"]
    movie = await _load_movie(job)
    original, localized, year = movie["original_title"], movie["localized_title"], movie["year"]

    needed = _needed_torrents(job)
    want_video = "video" in needed
    want_audio = "audio" in needed

    query_original = f"{original} {year}".strip()
    has_localized = bool(localized and localized.lower() != (original or "").lower())
    query_localized = f"{localized} {year}".strip() if has_localized else None

    # título em inglês: filmes cujo original NÃO é inglês (anime, cinema europeu...)
    # são indexados nos trackers pelo nome em inglês, não pelo original. Buscamos
    # por ele TAMBÉM e mesclamos na "versão original" (é o mesmo filme em áudio
    # original, só com outro nome). english_title vem None quando o original já
    # é inglês ou coincide — aí não há busca extra.
    english = movie.get("english_title")
    query_english = f"{english} {year}".strip() if english else None
    # títulos que identificam a versão original no matching: o original e (se
    # houver) o inglês — um release pode vir com qualquer um dos dois nomes
    original_titles = [original] + ([english] if english else [])

    # grafias do título localizado: o próprio + variantes de caractere especial
    # (& vs "e", @ vs a, ...). SÓ para o localizado (dublado), onde os trackers
    # BR bagunçam a grafia. O original fica como o TMDB dá (buscar "Fast e
    # Furious" não faz sentido). include_and=False: em português "and" é ruído.
    loc_spellings = ([localized] + selector.title_variants(localized, include_and=False)
                     if has_localized and want_audio else [])
    # as variantes (sem o título base, que já vira query_localized) buscadas com ano
    loc_variants = [f"{v} {year}".strip() for v in loc_spellings[1:]]

    # buscas extras direcionadas (só afetam o áudio dublado): cruzam CADA grafia
    # acima com as regras especiais (tirar ano, romano->arábico) e rodam em
    # paralelo por indexer configurado.
    extra_specs = _extra_searches(loc_spellings, year, lang) if want_audio else []

    _set(job, "searching", f"Procurando '{query_original}' no Jackett...")
    if query_english:
        _event(job, "search",
               f"Título original não é inglês — buscando também pelo nome em "
               f"inglês '{query_english}' (trackers indexam estrangeiros assim)")
    if extra_specs:
        _event(job, "search",
               f"{len(extra_specs)} busca(s) extra(s) configurada(s) para {label} "
               f"— rodando em paralelo")
    if loc_variants:
        _event(job, "search",
               f"Variantes de grafia do título em {label} — buscando também: "
               f"{', '.join(repr(v) for v in loc_variants)}")

    # dispara TODAS as buscas em paralelo. Guardamos os índices de cada grupo
    # para separar os resultados depois.
    tasks = [jackett.search(query_original)]
    i_english = None
    if query_english:
        i_english = len(tasks)
        tasks.append(jackett.search(query_english))
    if query_localized:
        tasks.append(jackett.search(query_localized))
    i_loc_var = len(tasks)
    for q in loc_variants:
        tasks.append(jackett.search(q))
    i_extra = len(tasks)
    for spec in extra_specs:
        tasks.append(_run_extra_search(job, spec))
    all_results = await asyncio.gather(*tasks)

    # a busca pelo nome em inglês é a MESMA versão original (áudio original, só
    # indexado com outro nome) — mescla no pool do original antes do dedup
    original_pool = list(all_results[0])
    if i_english is not None:
        eng_hits = all_results[i_english]
        original_pool.extend(eng_hits)
        _event(job, "search", f"Nome em inglês '{query_english}' → {len(eng_hits)} resultados")
    results_original = _dedup_results(original_pool)
    _event(job, "search", f"Jackett devolveu {len(results_original)} resultados para '{query_original}'"
           + (" (+ nome em inglês)" if query_english else ""))
    idx = 1 + (1 if query_english else 0)
    results_localized = []
    if query_localized:
        results_localized = all_results[idx]
        idx += 1
    loc_variant_results = all_results[i_loc_var:i_extra]
    extra_results = all_results[i_extra:]  # já logados dentro de _run_extra_search
    for q, r in zip(loc_variants, loc_variant_results):
        _event(job, "search", f"Variante '{q}' → {len(r)} resultados")

    # ---- audio dublado: titulo traduzido + titulo original com marcador ----
    audio_viable = []
    if want_audio:
        _set(job, "searching", f"Avaliando versão em {label}...")
        audio_ranked = []
        # resultados do título traduzido + variantes de grafia + buscas extras
        # entram como tier 0 (título no idioma dublado tem preferência máxima).
        # dedup: essas buscas repetem MUITO o mesmo release entre si.
        localized_pool = list(results_localized)
        for r in loc_variant_results:
            localized_pool.extend(r)
        for r in extra_results:
            localized_pool.extend(r)
        localized_pool = _dedup_results(localized_pool)
        # dubbed_title: passa o localizado só quando ≠ do original — aí
        # "título dublado + dual" conta como marcador forte (ver marker_strength)
        dubbed_title = localized if has_localized else None
        if localized_pool:
            ranked, trace = selector.rank(localized_pool, "audio", localized, year,
                                          language=lang, dubbed_title=dubbed_title)
            _event(job, "candidates", f"Avaliação para ÁUDIO — título em {label} (+ buscas extras)",
                   {"role": "audio", "query": query_localized or localized, "candidates": trace})
            for c in ranked:
                c["tier"] = 0  # titulo no idioma dublado: preferencia maxima
            audio_ranked.extend(ranked)

        ranked, trace = selector.rank(results_original, "audio", original_titles, year,
                                      language=lang, require_language=True,
                                      dubbed_title=dubbed_title)
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
        # dentro do tier, quem tem o ANO no nome vem antes (identificacao
        # confiavel — sem ano pode ser outro filme da franquia/remake); score
        # decide por ultimo. Mesma chave (identidade × provider) do
        # _dedup_results: mesmo torrent em trackers diferentes continua concorrendo.
        seen = set()
        for c in sorted(audio_ranked,
                        key=lambda r: (r.get("tier", 1),
                                       not r.get("year_match", False),
                                       -r["score"])):
            ident = _torrent_identity(c)
            key = (ident, c.get("tracker_id") or c.get("tracker"))
            if ident is not None and key in seen:
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
        video_viable, trace = selector.rank(results_original, "video", original_titles, year)
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
