"""Busca de torrents via API JSON do Jackett."""
import asyncio
import time
import xml.etree.ElementTree as ET

import httpx

import config

# categorias Torznab: 2000 = Movies, 5000 = TV
MOVIE_CATEGORIES = ["2000"]
TV_CATEGORIES = ["5000"]


async def search(query: str, indexer: str = "all",
                 categories: list[str] | None = None) -> list[dict]:
    """Busca no Jackett. indexer='all' varre todos; ou o id de um indexer só.

    categories: lista Torznab (default filmes). O pipeline de séries passa
    TV_CATEGORIES."""
    url = f"{config.JACKETT_URL}/api/v2.0/indexers/{indexer}/results"
    params = {
        "apikey": config.JACKETT_API_KEY,
        "Query": query,
        "Category[]": categories or MOVIE_CATEGORIES,
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
            # campos extras do pipeline de séries (aditivos; filmes ignoram):
            # nº de arquivos ajuda a suspeitar de pack incompatível (≠ da
            # contagem de episódios do TMDB); infohash ajuda no dedup
            "publish_date": item.get("PublishDate"),
            "files": item.get("Files"),
            "infohash": item.get("InfoHash"),
        })
    return results


async def list_indexers(configured_only: bool = True,
                        timeout: httpx.Timeout | None = None) -> list[dict]:
    """Lista os indexers do Jackett: [{id, name, language, configured}].

    Usa o feed Torznab de agregação (t=indexers), que aceita a apikey — o
    endpoint /api/v2.0/indexers do painel exige sessão de admin e redireciona
    para o login quando chamado só com a apikey.

    A lista dos configurados também renova o cache de configured_indexer_ids:
    abrir (ou recarregar) a tela de buscas extras depois de mexer no Jackett
    já vale para o próximo job.
    """
    url = f"{config.JACKETT_URL}/api/v2.0/indexers/all/results/torznab/api"
    params = {"apikey": config.JACKETT_API_KEY, "t": "indexers"}
    if configured_only:
        params["configured"] = "true"
    timeout = timeout or httpx.Timeout(connect=15, read=60, write=30, pool=30)
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
    if configured_only:
        _remember_ids(out)
    return out


# -------------------- ids configurados (cache curto) --------------------
# As buscas extras só consultam tracker que o Jackett ainda tem configurado
# (services/jobs/search.py): um tracker removido do Jackett continuava nas
# regras e cada job gastava uma busca nele só para colher o erro.
#
# TTL de 5 min: a lista só muda quando alguém mexe no Jackett (ação humana,
# rara), e um lote de filmes criado de uma vez dispara as buscas de todos em
# segundos; com o cache o lote inteiro custa UMA listagem. O preço de ficar
# velho é pequeno nos dois sentidos: tracker que saiu ainda é tentado por até
# 5 min (o comportamento antigo, só um evento de erro), e tracker que voltou é
# pulado por até 5 min com o aviso no job — e abrir a tela de buscas extras
# renova o cache na hora (list_indexers acima).
# Falha da listagem fica guardada só 30 s: é o bastante para os jobs de um
# lote não esperarem cada um o seu timeout, e curto para voltar a filtrar
# logo que o Jackett responder.
IDS_TTL_S = 300.0
IDS_FAIL_TTL_S = 30.0
# t=indexers sai da configuração em memória do Jackett (nenhum tracker é
# consultado): responde em milissegundos. Teto curto para um Jackett doente
# não segurar o começo da busca pelos 60 s do timeout da tela.
_IDS_TIMEOUT = httpx.Timeout(connect=5, read=15, write=15, pool=15)

_ids_cache: tuple[float, frozenset[str] | None] | None = None  # (quando, ids)
_ids_lock: asyncio.Lock | None = None
_ids_lock_loop = None


def _remember_ids(indexers: list[dict]) -> frozenset[str] | None:
    """Guarda os ids da listagem. Lista vazia vira None (= não sei): Jackett
    sem nenhum indexer não acha nada de qualquer jeito, e uma resposta vazia
    de um Jackett recém-subido não pode tirar das buscas os trackers bons."""
    global _ids_cache
    ids = frozenset(i["id"] for i in indexers if i.get("id")) or None
    _ids_cache = (time.monotonic(), ids)
    return ids


def _get_ids_lock() -> asyncio.Lock:
    """Um lock por event loop (os testes criam um loop por asyncio.run; um
    Lock preso a um loop fechado estoura no próximo)."""
    global _ids_lock, _ids_lock_loop
    loop = asyncio.get_running_loop()
    if _ids_lock is None or _ids_lock_loop is not loop:
        _ids_lock, _ids_lock_loop = asyncio.Lock(), loop
    return _ids_lock


async def configured_indexer_ids() -> frozenset[str] | None:
    """Ids dos indexers configurados no Jackett, com o cache acima.

    None = não deu para saber (listagem falhou, ou veio vazia): quem chama NÃO
    descarta nada — sem a lista não dá para dizer o que sumiu, e falhar fechado
    deixaria de buscar em tracker bom. O lock faz os jobs que chegam juntos
    esperarem a mesma listagem em vez de cada um fazer a sua."""
    async with _get_ids_lock():
        if _ids_cache is not None:
            ts, ids = _ids_cache
            ttl = IDS_TTL_S if ids is not None else IDS_FAIL_TTL_S
            if time.monotonic() - ts < ttl:
                return ids
        try:
            indexers = await list_indexers(configured_only=True, timeout=_IDS_TIMEOUT)
        except Exception:  # noqa: BLE001 - Jackett fora/XML ruim: só não filtra
            indexers = []
        return _remember_ids(indexers)
