"""Pasta com [tmdbid-N] deve consultar o TMDB pelo ID, não adivinhar pelo título.

O ID no nome da pasta foi escolhido (ou confirmado) por alguém; refazer a busca
textual erraria justamente nos casos que o ID existe para resolver — remake,
título localizado, coleção. Aqui não há rede: as chamadas ao TMDB são
substituídas para observar QUAL caminho a rota escolhe.
"""
import asyncio

import main
from services import catalog


def _detail(tmdb_id, title="Filme Exemplo", year="2021"):
    return {"tmdb_id": tmdb_id, "title": title, "year": year,
            "destination": {}, "folder": "x", "files": []}


def _route(monkeypatch, detail):
    """Chama a rota com item_detail fixo e registra o que foi consultado."""
    calls = {}

    async def fake_by_id(movie_id):
        calls["by_id"] = movie_id
        return {"id": movie_id, "title": "pelo id"}

    async def fake_match(title, year=None):
        calls["match"] = (title, year)
        return {"id": 999, "title": "pelo titulo"}

    monkeypatch.setattr(catalog, "item_detail", lambda *_a, **_k: detail)
    monkeypatch.setattr(main.tmdb, "by_id", fake_by_id)
    monkeypatch.setattr(main.tmdb, "match", fake_match)
    result = asyncio.run(main.catalog_item(folder="x", destination_id=None))
    return result, calls


def test_usa_o_id_quando_a_pasta_esta_marcada(monkeypatch):
    result, calls = _route(monkeypatch, _detail(438631))
    assert calls == {"by_id": 438631}          # nada de busca textual
    assert result["tmdb"]["title"] == "pelo id"


def test_cai_para_o_titulo_sem_id(monkeypatch):
    result, calls = _route(monkeypatch, _detail(None))
    assert calls == {"match": ("Filme Exemplo", "2021")}
    assert result["tmdb"]["title"] == "pelo titulo"


def test_id_inexistente_nao_quebra_a_pagina(monkeypatch):
    """by_id devolvendo None (404 no TMDB) não pode virar erro na tela."""
    async def none_by_id(_movie_id):
        return None
    monkeypatch.setattr(catalog, "item_detail", lambda *_a, **_k: _detail(1))
    monkeypatch.setattr(main.tmdb, "by_id", none_by_id)
    result = asyncio.run(main.catalog_item(folder="x", destination_id=None))
    assert result["tmdb"] is None


def test_falha_de_rede_nao_quebra_a_pagina(monkeypatch):
    async def boom(*_a, **_k):
        raise RuntimeError("sem rede")
    monkeypatch.setattr(catalog, "item_detail", lambda *_a, **_k: _detail(1))
    monkeypatch.setattr(main.tmdb, "by_id", boom)
    result = asyncio.run(main.catalog_item(folder="x", destination_id=None))
    assert result["tmdb"] is None
