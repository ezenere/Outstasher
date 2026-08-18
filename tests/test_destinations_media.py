"""Destinos por mídia: filmes e séries têm bibliotecas SEPARADAS.

Cobre a migração da coluna `media` (bancos antigos), o padrão POR mídia e a
recusa de destino da mídia errada na criação de jobs.
"""
import sqlite3

import pytest


def test_padrao_por_midia(temp_db):
    m = temp_db.add_destination("Filmes", "/m", True, "movie")
    t = temp_db.add_destination("Séries", "/t", True, "tv")
    # marcar o padrão de séries NÃO desmarca o de filmes
    assert temp_db.default_destination("movie")["id"] == m["id"]
    assert temp_db.default_destination("tv")["id"] == t["id"]
    # e vice-versa: um novo padrão de filmes não mexe no de séries
    m2 = temp_db.add_destination("Filmes 2", "/m2", True, "movie")
    assert temp_db.default_destination("movie")["id"] == m2["id"]
    assert temp_db.default_destination("tv")["id"] == t["id"]


def test_listagem_filtrada(temp_db):
    temp_db.add_destination("Filmes", "/m", True, "movie")
    temp_db.add_destination("Séries", "/t", True, "tv")
    movies = temp_db.list_destinations_by_media("movie")
    tv = temp_db.list_destinations_by_media("tv")
    assert all(d["media"] == "movie" for d in movies)
    assert all(d["media"] == "tv" for d in tv)
    # a listagem geral traz as duas, com o campo media presente
    assert {d["media"] for d in temp_db.list_destinations()} >= {"movie", "tv"}


def test_migracao_banco_antigo(tmp_path, monkeypatch):
    """Banco criado ANTES da coluna media: o init faz o ALTER e os destinos
    existentes viram 'movie' (era o único uso possível)."""
    db = tmp_path / "jobs.db"
    conn = sqlite3.connect(db)
    conn.execute("""
        CREATE TABLE destinations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT NOT NULL,
            path TEXT NOT NULL,
            is_default INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )""")
    conn.execute("INSERT INTO destinations (label, path, is_default, created_at) "
                 "VALUES ('Antigo', '/velho', 1, '2026-01-01')")
    conn.commit()
    conn.close()

    import importlib
    monkeypatch.setenv("DB_DIR", str(tmp_path))
    import config
    importlib.reload(config)
    from services import store
    importlib.reload(store)
    store.init()

    old = store.list_destinations()
    assert old[0]["label"] == "Antigo"
    assert old[0]["media"] == "movie"
    assert store.default_destination("movie")["label"] == "Antigo"
    assert store.default_destination("tv") is None


def test_job_de_serie_recusa_destino_de_filme(temp_db):
    import asyncio
    from services.series import pipeline
    m = temp_db.add_destination("Filmes", "/m", True, "movie")
    with pytest.raises(ValueError, match="FILMES"):
        asyncio.run(pipeline.create_series(1, "pt", seasons=[1],
                                           destination_id=m["id"]))


def test_job_de_serie_sem_destino_tv_explica(temp_db):
    import asyncio
    from services.series import pipeline
    temp_db.add_destination("Filmes", "/m", True, "movie")
    with pytest.raises(ValueError, match="SÉRIES"):
        asyncio.run(pipeline.create_series(1, "pt", seasons=[1]))


def test_job_de_filme_recusa_destino_de_serie(temp_db):
    import asyncio
    from services import jobs
    t = temp_db.add_destination("Séries", "/t", True, "tv")
    with pytest.raises(ValueError, match="SÉRIES"):
        asyncio.run(jobs.create(1, "pt", destination_id=t["id"]))