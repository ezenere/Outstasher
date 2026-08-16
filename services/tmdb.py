"""Cliente minimo da API do TMDB."""
import httpx

import config

BASE = "https://api.themoviedb.org/3"


def _params(extra: dict | None = None) -> dict:
    p = dict(extra or {})
    # Chave v3 vai como query param; token v4 vai no header (ver _headers)
    if config.TMDB_API_KEY and not config.TMDB_API_KEY.startswith("ey"):
        p["api_key"] = config.TMDB_API_KEY
    return p


def _headers() -> dict:
    if config.TMDB_API_KEY.startswith("ey"):  # token v4 (JWT)
        return {"Authorization": f"Bearer {config.TMDB_API_KEY}"}
    return {}


async def _get(path: str, params: dict | None = None) -> dict:
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(BASE + path, params=_params(params), headers=_headers())
        r.raise_for_status()
        return r.json()


def _slim(movie: dict) -> dict:
    return {
        "id": movie["id"],
        "title": movie.get("title"),
        "original_title": movie.get("original_title"),
        "year": (movie.get("release_date") or "")[:4],
        "overview": movie.get("overview"),
        "poster": f"https://image.tmdb.org/t/p/w342{movie['poster_path']}"
        if movie.get("poster_path") else None,
        "rating": movie.get("vote_average"),
    }


def _page(data: dict) -> dict:
    """Empacota resultados + metadados de paginação do TMDB."""
    return {
        "results": [_slim(m) for m in data.get("results", [])],
        "page": data.get("page", 1),
        "total_pages": data.get("total_pages", 1),
        "total_results": data.get("total_results", 0),
    }


def _slim_tv(tv: dict) -> dict:
    """Série no MESMO shape do filme (title/original_title/year) para os cards
    da busca reutilizarem tipo e renderização — a API de TV do TMDB usa
    name/original_name/first_air_date."""
    return {
        "id": tv["id"],
        "title": tv.get("name"),
        "original_title": tv.get("original_name"),
        "year": (tv.get("first_air_date") or "")[:4],
        "overview": tv.get("overview"),
        "poster": f"https://image.tmdb.org/t/p/w342{tv['poster_path']}"
        if tv.get("poster_path") else None,
        "rating": tv.get("vote_average"),
        "media_type": "tv",
    }


def _page_tv(data: dict) -> dict:
    return {
        "results": [_slim_tv(m) for m in data.get("results", [])],
        "page": data.get("page", 1),
        "total_pages": data.get("total_pages", 1),
        "total_results": data.get("total_results", 0),
    }


async def popular(page: int = 1) -> dict:
    data = await _get("/movie/popular", {"page": page, "language": "pt-BR"})
    return _page(data)


async def search(query: str, page: int = 1) -> dict:
    data = await _get("/search/movie", {"query": query, "page": page, "language": "pt-BR"})
    return _page(data)


async def popular_tv(page: int = 1) -> dict:
    data = await _get("/tv/popular", {"page": page, "language": "pt-BR"})
    return _page_tv(data)


async def search_tv(query: str, page: int = 1) -> dict:
    data = await _get("/search/tv", {"query": query, "page": page, "language": "pt-BR"})
    return _page_tv(data)


async def by_id(movie_id: int) -> dict | None:
    """Filme pelo id exato do TMDB. None se o id nao existe (404).

    Quando a pasta ja tem [tmdbid-N], este e o caminho certo: o id foi escolhido
    (ou confirmado) por alguem, entao adivinhar de novo pelo titulo so poderia
    errar — e erraria justamente nos casos que o id resolve (remake, titulo
    localizado, colecao).
    """
    try:
        return _slim(await _get(f"/movie/{movie_id}", {"language": "pt-BR"}))
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return None
        raise


async def match(title: str, year: str | None = None) -> dict | None:
    """Melhor palpite de filme por titulo (+ ano) para o catalogo. None se nada bate."""
    params = {"query": title, "language": "pt-BR"}
    if year:
        params["year"] = year
    data = await _get("/search/movie", params)
    results = data.get("results") or []
    if not results and year:  # tenta de novo sem o ano
        data = await _get("/search/movie", {"query": title, "language": "pt-BR"})
        results = data.get("results") or []
    return _slim(results[0]) if results else None


def _english_translation(data: dict, field: str, original: str) -> str | None:
    """Texto em inglês de translations (append_to_response). None se não houver
    ou se coincidir com o original (nada a ganhar). `field` é "title" para
    filmes e "name" para séries — a API de TV traduz `name`, não `title`."""
    if (data.get("original_language") or "").lower() == "en":
        return None  # o original já é o inglês
    for tr in ((data.get("translations") or {}).get("translations") or []):
        if (tr.get("iso_639_1") or "").lower() == "en":
            text = ((tr.get("data") or {}).get(field) or "").strip()
            if text and text.lower() != original.lower():
                return text
    return None


def _english_title(data: dict) -> str | None:
    """Título em inglês do filme. Trackers costumam indexar filmes estrangeiros
    pelo nome em inglês, não pelo original — daí buscar por ele também na
    versão original (ver jobs._search)."""
    return _english_translation(data, "title",
                                (data.get("original_title") or "").strip())


async def details(movie_id: int, language: str) -> dict:
    """Retorna titulo original, titulo traduzido no idioma pedido e (para filmes
    nao-ingleses) o titulo em ingles — trackers indexam estrangeiros pelo nome
    em ingles. O append_to_response traz as traducoes na MESMA requisicao."""
    tmdb_lang = config.LANGUAGES[language]["tmdb"]
    data = await _get(f"/movie/{movie_id}",
                      {"language": tmdb_lang, "append_to_response": "translations"})
    return {
        "id": data["id"],
        "original_title": data.get("original_title"),
        "localized_title": data.get("title"),
        # título em inglês (só quando o original não é inglês e difere); None senão
        "english_title": _english_title(data),
        # ISO 639-1 ("en", "ja"...): usado pelo filtro "apenas original + dublagem"
        "original_language": data.get("original_language"),
        "year": (data.get("release_date") or "")[:4],
        "overview": data.get("overview"),
        "poster": f"https://image.tmdb.org/t/p/w342{data['poster_path']}"
        if data.get("poster_path") else None,
    }


# -------------------- séries (TV) --------------------

async def tv_details(tv_id: int, language: str | None = None) -> dict:
    """Detalhe da série com a lista de temporadas. Mesmo contrato do details()
    de filme (original/localized/english title) + `seasons`, servindo tanto a
    UI de descoberta quanto a busca de torrents do pipeline de séries.

    `language` é a chave de config.LANGUAGES (idioma da dublagem); sem ela, a
    localização fica em pt-BR (rota de navegação).
    """
    tmdb_lang = config.LANGUAGES[language]["tmdb"] if language else "pt-BR"
    data = await _get(f"/tv/{tv_id}",
                      {"language": tmdb_lang, "append_to_response": "translations"})
    original = (data.get("original_name") or "").strip()
    return {
        "id": data["id"],
        "original_title": data.get("original_name"),
        "localized_title": data.get("name"),
        "english_title": _english_translation(data, "name", original),
        "original_language": data.get("original_language"),
        "year": (data.get("first_air_date") or "")[:4],
        "overview": data.get("overview"),
        "poster": f"https://image.tmdb.org/t/p/w342{data['poster_path']}"
        if data.get("poster_path") else None,
        "seasons": [{
            "season": s.get("season_number"),
            "name": s.get("name"),
            "episode_count": s.get("episode_count"),
            "air_date": s.get("air_date"),
        } for s in (data.get("seasons") or [])],
    }


async def tv_season(tv_id: int, season: int) -> dict:
    """Episódios de uma temporada. air_date alimenta a regra de "episódio ainda
    não lançado" (skipped_future) e a UI de seleção."""
    data = await _get(f"/tv/{tv_id}/season/{season}", {"language": "pt-BR"})
    return {
        "season": data.get("season_number", season),
        "name": data.get("name"),
        "episodes": [{
            "episode": e.get("episode_number"),
            "name": e.get("name"),
            "air_date": e.get("air_date"),
            "runtime": e.get("runtime"),
            "overview": e.get("overview"),
        } for e in (data.get("episodes") or [])],
    }


async def tv_episode_groups(tv_id: int) -> list[dict]:
    """Ordens alternativas de episódios (exibição/DVD/absoluta...). Uma série
    com grupos cujo total de episódios difere da ordem padrão é candidata a
    conflito de ordem entre mídias (TV vs Blu-ray) — sinal para o gate de
    torrents incompatíveis, nunca resolução automática."""
    data = await _get(f"/tv/{tv_id}/episode_groups")
    return [{
        "id": g.get("id"),
        "name": g.get("name"),
        "type": g.get("type"),  # 1=exibição 2=absoluta 3=DVD 4=digital ... 7=TV
        "group_count": g.get("group_count"),
        "episode_count": g.get("episode_count"),
    } for g in (data.get("results") or [])]


async def tv_episode_group(group_id: str) -> dict:
    """Detalhe de um episode_group: os grupos (temporadas da ordem alternativa)
    com seus episódios (id + número na ordem padrão + ordem no grupo). É o
    mapa usado para remapear os episódios pedidos quando o usuário escolhe uma
    ordem alternativa no gate."""
    data = await _get(f"/tv/episode_group/{group_id}")
    return {
        "id": data.get("id"),
        "name": data.get("name"),
        "groups": [{
            "name": g.get("name"),
            "order": g.get("order"),
            "episodes": [{
                "id": e.get("id"),
                "season": e.get("season_number"),
                "episode": e.get("episode_number"),
                "order": e.get("order"),
                "name": e.get("name"),
            } for e in (g.get("episodes") or [])],
        } for g in (data.get("groups") or [])],
    }
