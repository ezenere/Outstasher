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


async def popular(page: int = 1) -> dict:
    data = await _get("/movie/popular", {"page": page, "language": "pt-BR"})
    return _page(data)


async def search(query: str, page: int = 1) -> dict:
    data = await _get("/search/movie", {"query": query, "page": page, "language": "pt-BR"})
    return _page(data)


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


async def details(movie_id: int, language: str) -> dict:
    """Retorna titulo original e titulo traduzido no idioma pedido."""
    tmdb_lang = config.LANGUAGES[language]["tmdb"]
    data = await _get(f"/movie/{movie_id}", {"language": tmdb_lang})
    return {
        "id": data["id"],
        "original_title": data.get("original_title"),
        "localized_title": data.get("title"),
        # ISO 639-1 ("en", "ja"...): usado pelo filtro "apenas original + dublagem"
        "original_language": data.get("original_language"),
        "year": (data.get("release_date") or "")[:4],
        "overview": data.get("overview"),
        "poster": f"https://image.tmdb.org/t/p/w342{data['poster_path']}"
        if data.get("poster_path") else None,
    }
