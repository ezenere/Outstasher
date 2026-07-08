"""Busca de torrents via API JSON do Jackett."""
import xml.etree.ElementTree as ET

import httpx

import config

# 2000 = categoria Movies do Torznab
MOVIE_CATEGORIES = ["2000"]


async def search(query: str, indexer: str = "all") -> list[dict]:
    """Busca no Jackett. indexer='all' varre todos; ou o id de um indexer só."""
    url = f"{config.JACKETT_URL}/api/v2.0/indexers/{indexer}/results"
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
            "tracker_id": item.get("TrackerId"),  # slug estavel do indexer
        })
    return results


async def list_indexers(configured_only: bool = True) -> list[dict]:
    """Lista os indexers do Jackett: [{id, name, language, configured}].

    Usa o feed Torznab de agregação (t=indexers), que aceita a apikey — o
    endpoint /api/v2.0/indexers do painel exige sessão de admin e redireciona
    para o login quando chamado só com a apikey.
    """
    url = f"{config.JACKETT_URL}/api/v2.0/indexers/all/results/torznab/api"
    params = {"apikey": config.JACKETT_API_KEY, "t": "indexers"}
    if configured_only:
        params["configured"] = "true"
    timeout = httpx.Timeout(connect=15, read=60, write=30, pool=30)
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.get(url, params=params)
        r.raise_for_status()
        text = r.text

    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        raise RuntimeError(f"resposta do Jackett não é XML válido: {e}")
    out = []
    for it in root.findall("indexer"):
        iid = it.get("id")
        if not iid:
            continue
        title = it.findtext("title") or iid
        language = it.findtext("language") or ""
        out.append({"id": iid, "name": title, "language": language,
                    "configured": it.get("configured") == "true"})
    out.sort(key=lambda x: x["name"].lower())
    return out
