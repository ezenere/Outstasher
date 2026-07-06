"""Busca de torrents via API JSON do Jackett (todos os indexadores)."""
import httpx

import config

# 2000 = categoria Movies do Torznab
MOVIE_CATEGORIES = ["2000"]


async def search(query: str) -> list[dict]:
    url = f"{config.JACKETT_URL}/api/v2.0/indexers/all/results"
    params = {
        "apikey": config.JACKETT_API_KEY,
        "Query": query,
        "Category[]": MOVIE_CATEGORIES,
    }
    # Jackett pode levar 5-10 min consultando os indexadores — leitura com teto de 20 min
    timeout = httpx.Timeout(connect=15, read=1200, write=30, pool=30)
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.get(url, params=params)
        r.raise_for_status()
        data = r.json()

    results = []
    for item in data.get("Results", []):
        results.append({
            "title": item.get("Title") or "",
            "seeders": item.get("Seeders") or 0,
            "size": item.get("Size") or 0,
            "magnet": item.get("MagnetUri"),
            "link": item.get("Link"),  # .torrent (fallback quando nao ha magnet)
            "tracker": item.get("Tracker"),
        })
    return results
