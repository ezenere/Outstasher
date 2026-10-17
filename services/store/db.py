"""Conexão com o SQLite e init().

init() abre o banco (WAL, FKs ligadas), cria o schema base (jobs, events,
destinos, alvos de torrent, settings, presets, idiomas), chama a criação das
tabelas de cada seção, as migrações de colunas e de listas e os seeds.

A conexão e o lock moram no pacote (services/store/__init__.py) e são lidos
aqui como `store._conn` / `store._lock` NA HORA da chamada.
"""
import sqlite3

import config
from services import store

from .candidates import _create_candidate_tables
from .configuration import (
    _create_config_list_tables, _migrate_config_lists,
    _seed_default_destination, _seed_default_torrent_target, _seed_languages,
    load_language_config,
)
from .edls import _create_edl_tables
from .jobs import _migrate_from_json
from .scalar_rows import _create_scalar_tables
from .series import _create_series_tables


def init():
    if store._conn is not None:
        return
    # garante o diretorio do banco (ex.: /data montado como volume no Docker)
    config.DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    store._conn = sqlite3.connect(config.DB_FILE, check_same_thread=False)
    store._conn.execute("PRAGMA journal_mode=WAL")
    store._conn.execute("PRAGMA synchronous=NORMAL")
    # o SQLite vem com FKs DESLIGADAS por conexão: sem isto o ON DELETE
    # CASCADE das tabelas filhas do job não roda e remover job deixa órfãos
    store._conn.execute("PRAGMA foreign_keys = ON")
    store._conn.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            status TEXT NOT NULL,
            data TEXT NOT NULL
        )""")
    store._conn.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL,
            ts TEXT NOT NULL,
            kind TEXT NOT NULL,
            message TEXT NOT NULL,
            data TEXT
        )""")
    store._conn.execute("CREATE INDEX IF NOT EXISTS idx_events_job ON events(job_id)")
    store._conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status)")
    store._conn.execute("""
        CREATE TABLE IF NOT EXISTS destinations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT NOT NULL,
            path TEXT NOT NULL,
            media TEXT NOT NULL DEFAULT 'movie',
            is_default INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )""")
    # migração de bancos anteriores à separação filme/série: a coluna `media`
    # aparece via ALTER (CREATE IF NOT EXISTS não altera tabela existente).
    # Destinos antigos viram 'movie' — era o único uso possível até então.
    cols = {r[1] for r in store._conn.execute("PRAGMA table_info(destinations)")}
    if "media" not in cols:
        store._conn.execute("ALTER TABLE destinations "
                      "ADD COLUMN media TEXT NOT NULL DEFAULT 'movie'")
    store._conn.execute("""
        CREATE TABLE IF NOT EXISTS torrent_targets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT NOT NULL,
            save_path TEXT NOT NULL DEFAULT '',
            local_path TEXT NOT NULL DEFAULT '',
            is_default INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )""")
    # tabela chave/valor (senha, api key, e o que mais precisar guardar)
    store._conn.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )""")
    # presets de conversao (ConvertOptions nomeadas). name UNIQUE: salvar com um
    # nome existente sobrescreve, em vez de duplicar a entrada na lista.
    store._conn.execute("""
        CREATE TABLE IF NOT EXISTS convert_presets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            options TEXT NOT NULL
        )""")
    # idiomas da faixa dublada (editaveis pela UI). Os marcadores moram em
    # language_markers; as colunas markers_* (JSON) são do formato antigo —
    # lidas uma vez por _migrate_config_lists e sem uso depois.
    store._conn.execute("""
        CREATE TABLE IF NOT EXISTS languages (
            code TEXT PRIMARY KEY,
            label TEXT NOT NULL,
            tmdb TEXT NOT NULL,
            markers_strong TEXT NOT NULL DEFAULT '[]',
            markers_weak TEXT NOT NULL DEFAULT '[]',
            sort_order INTEGER NOT NULL DEFAULT 0
        )""")
    _create_config_list_tables()
    _create_candidate_tables()
    _create_series_tables()
    _create_edl_tables()
    _create_scalar_tables()   # colunas da jobs + idx_jobs_media + metadados
    store._conn.commit()
    _migrate_from_json()
    _seed_default_destination()
    _seed_default_torrent_target()
    _migrate_config_lists()   # antes do seed: banco novo não tem o que migrar
    _seed_languages()
    load_language_config()
