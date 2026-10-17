"""Escalares do job no banco: o SQL das linhas que os codecs (scalars) montam.

As colunas da `jobs` (ALTER em bancos antigos) e as tabelas filhas e
compartilhadas (_create_scalar_tables), a gravação das que mudaram, o SELECT
da `jobs` com os JOINs dos metadados e do progresso (_JOB_SELECT), a leitura
das tabelas filhas e a linha -> documento (_row_doc).
"""
import json

from services import store

from .common import _in_chunks
from .scalars import (
    _JCOLS, _JOB_COLUMNS, _KID_DECL, _KID_OWNER, _KID_TABLES, _MASK_BIT,
    _MEDIA_BASE, _MEDIA_LOCAL, _PROG_NAMES, _ROW_KEYS, _decode,
)


def _create_scalar_tables():
    cols = {r[1] for r in store._conn.execute("PRAGMA table_info(jobs)")}
    for col, decl in _JOB_COLUMNS:
        if col not in cols:
            store._conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} {decl}")
    # o índice de mídia era sobre json_extract(data, '$.media_type'): agora
    # é a coluna (o filtro de mídia das listagens e das contagens)
    old = store._conn.execute("SELECT sql FROM sqlite_master WHERE type = 'index' "
                        "AND name = 'idx_jobs_media'").fetchone()
    if old and "json_extract" in (old[0] or ""):
        store._conn.execute("DROP INDEX idx_jobs_media")
    store._conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_media "
                  "ON jobs(media_type, status)")
    # linhas do formato antigo (tudo no documento) também precisam da coluna
    # de mídia certa para os filtros; o resto da linha fica para a migração
    store._conn.execute(
        "UPDATE jobs SET media_type = 'tv' WHERE keys IS NULL "
        "AND media_type != 'tv' AND CASE WHEN json_valid(data) "
        "THEN json_extract(data, '$.media_type') END = 'tv'")
    for table, kcols in _KID_TABLES.items():
        decl = ", ".join(f"{c} {_KID_DECL.get(c, 'TEXT')}" for c in kcols)
        store._conn.execute(f"""
            CREATE TABLE IF NOT EXISTS {table} (
                job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                {decl},
                PRIMARY KEY (job_id, {kcols[0]})
            )""")
    store._conn.execute("""
        CREATE TABLE IF NOT EXISTS media (
            tmdb_id INTEGER NOT NULL,
            media_type TEXT NOT NULL,
            original_title TEXT,
            english_title TEXT,
            original_language TEXT,
            year TEXT,
            PRIMARY KEY (tmdb_id, media_type)
        )""")
    store._conn.execute("""
        CREATE TABLE IF NOT EXISTS media_localized (
            tmdb_id INTEGER NOT NULL,
            media_type TEXT NOT NULL,
            language TEXT NOT NULL,
            localized_title TEXT,
            overview TEXT,
            poster TEXT,
            PRIMARY KEY (tmdb_id, media_type, language),
            FOREIGN KEY (tmdb_id, media_type)
                REFERENCES media(tmdb_id, media_type) ON DELETE CASCADE
        )""")
    # só séries (temporada não existe em filme): tmdb_id de série
    store._conn.execute("""
        CREATE TABLE IF NOT EXISTS media_seasons (
            tmdb_id INTEGER NOT NULL,
            season INTEGER NOT NULL,
            PRIMARY KEY (tmdb_id, season)
        )""")
    store._conn.execute("""
        CREATE TABLE IF NOT EXISTS media_episode_groups (
            tmdb_id INTEGER NOT NULL,
            pos INTEGER NOT NULL,
            group_id TEXT,
            name TEXT,
            type INTEGER,
            group_count INTEGER,
            episode_count INTEGER,
            PRIMARY KEY (tmdb_id, pos)
        )""")


def _write_scalar_rows(job_id: str, enc: dict, digests: dict,
                       cached: dict | None):
    """Grava as tabelas filhas e compartilhadas que mudaram (com _lock, na
    transação do upsert)."""
    cached = cached or {}
    for table, kcols in _KID_TABLES.items():
        if cached.get(table) == digests[table]:
            continue
        store._conn.execute(f"DELETE FROM {table} WHERE job_id = ?", (job_id,))
        rows = enc["kids"][table]
        if rows:
            ph = ", ".join("?" for _ in range(len(kcols) + 1))
            store._conn.executemany(
                f"INSERT INTO {table} (job_id, {', '.join(kcols)}) VALUES ({ph})",
                [(job_id, *r) for r in rows])
    shared = enc["shared"]
    if "media" in shared and cached.get("media") != digests["media"]:
        base, loc = shared["media"]
        # upsert e não INSERT OR REPLACE: o REPLACE apagaria a linha e, pelo
        # CASCADE, os títulos localizados dos OUTROS idiomas
        upd = ", ".join(f"{k} = excluded.{k}" for k in _MEDIA_BASE)
        store._conn.execute(
            f"INSERT INTO media (tmdb_id, media_type, {', '.join(_MEDIA_BASE)}) "
            f"VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(tmdb_id, media_type) "
            f"DO UPDATE SET {upd}", base)
        upd = ", ".join(f"{k} = excluded.{k}" for k in _MEDIA_LOCAL)
        store._conn.execute(
            f"INSERT INTO media_localized (tmdb_id, media_type, language, "
            f"{', '.join(_MEDIA_LOCAL)}) VALUES (?, ?, ?, ?, ?, ?) "
            f"ON CONFLICT(tmdb_id, media_type, language) DO UPDATE SET {upd}", loc)
    if "seasons" in shared and cached.get("seasons") != digests["seasons"]:
        tmdb, seasons = shared["seasons"]
        store._conn.execute("DELETE FROM media_seasons WHERE tmdb_id = ?", (tmdb,))
        store._conn.executemany(
            "INSERT INTO media_seasons (tmdb_id, season) VALUES (?, ?)",
            [(tmdb, s) for s in seasons])
    if "groups" in shared and cached.get("groups") != digests["groups"]:
        tmdb, groups = shared["groups"]
        store._conn.execute("DELETE FROM media_episode_groups WHERE tmdb_id = ?", (tmdb,))
        store._conn.executemany(
            "INSERT INTO media_episode_groups (tmdb_id, pos, group_id, name, "
            "type, group_count, episode_count) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(tmdb, *g) for g in groups])


# o SELECT das linhas de `jobs` com as colunas do JOIN de media*
_JOB_SELECT_NAMES = (("id", "created_at", "status", "data") + _JCOLS
                     + tuple(f"m_{k}" for k in _MEDIA_BASE + _MEDIA_LOCAL)
                     + _PROG_NAMES)
_JOB_SELECT = (
    "SELECT " + ", ".join(f"j.{c}" for c in ("id", "created_at", "status",
                                               "data") + _JCOLS)
    + ", " + ", ".join(f"m.{k}" for k in _MEDIA_BASE)
    + ", " + ", ".join(f"ml.{k}" for k in _MEDIA_LOCAL)
    + ", " + ", ".join(n.replace("_", ".", 1) for n in _PROG_NAMES)
    + " FROM jobs j"
    " LEFT JOIN media m ON j.media_ref = 1 AND m.tmdb_id = j.tmdb_id"
    " AND m.media_type = j.media_type"
    " LEFT JOIN media_localized ml ON j.media_ref = 1"
    " AND ml.tmdb_id = j.tmdb_id AND ml.media_type = j.media_type"
    " AND ml.language = j.language"
    " LEFT JOIN job_progress pv ON pv.job_id = j.id AND pv.role = 'video'"
    " LEFT JOIN job_progress pa ON pa.job_id = j.id AND pa.role = 'audio'")


def _load_scalar_kids(rows: list[dict]) -> dict[str, dict[str, list]]:
    """{job_id: {tabela: linhas}} das tabelas filhas e compartilhadas, para
    as linhas (já como dict) do formato novo (com _lock). Tabela que nenhum
    job do lote usa nem é consultada; job sem linhas fica fora do dict."""
    out: dict[str, dict[str, list]] = {}
    for table, owner in _KID_OWNER.items():
        ids = [c["id"] for c in rows if c["keys"] & _MASK_BIT[owner]]
        cols = ", ".join(_KID_TABLES[table])
        for chunk, ph in _in_chunks(ids):
            for r in store._conn.execute(
                    f"SELECT job_id, {cols} FROM {table} WHERE job_id IN ({ph}) "
                    f"ORDER BY job_id, {_KID_TABLES[table][0]}", chunk):
                out.setdefault(r[0], {}).setdefault(table, []).append(r[1:])
    for table, owner, cols in (
            ("media_seasons", "known_seasons", "season"),
            ("media_episode_groups", "episode_groups",
             "pos, group_id, name, type, group_count, episode_count")):
        by_tmdb: dict[int, list] = {}
        tmdbs = sorted({c["tmdb_id"] for c in rows
                        if c["keys"] & _MASK_BIT[owner]
                        and c["tmdb_id"] is not None})
        for chunk, ph in _in_chunks(tmdbs):
            for r in store._conn.execute(
                    f"SELECT tmdb_id, {cols} FROM {table} WHERE tmdb_id IN ({ph}) "
                    f"ORDER BY tmdb_id, {cols.split(',')[0]}", chunk):
                by_tmdb.setdefault(r[0], []).append(r[1:])
        for c in rows:
            if c["keys"] & _MASK_BIT[owner] and c["tmdb_id"] in by_tmdb:
                out.setdefault(c["id"], {})[table] = by_tmdb[c["tmdb_id"]]
    return out


def _row_doc(c: dict, kids: dict | None) -> dict:
    """Linha de `jobs` (como dict) -> documento do job: as colunas das chaves
    marcadas em `keys` e, por cima, o que ficou em jobs.data."""
    if c["keys"] is None:                 # formato antigo: tudo no documento
        return json.loads(c["data"])
    doc = {k: c[k] for k in _ROW_KEYS}
    doc.update(_decode(c, kids))
    doc.update(json.loads(c["data"]))
    return doc
