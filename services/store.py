"""Persistencia em SQLite (jobs.db).

- jobs: uma linha por job — os escalares em colunas e, em jobs.data, só a
  configuração (convert, advanced_merge, review_rules) e o que não coube no
  formato; as listas padronizadas em tabelas próprias (ver upsert_job):
  search_queries / search_candidates / candidate_episodes (candidatos) e,
  nos jobs de série, job_episodes / episode_subs / job_torrents /
  torrent_coverage / torrent_files; as EDLs do alinhador (série e filme) em
  edls / edl_segments; progresso do download do filme, pedido da série,
  merge manual, lote de recompressão e perfil de drift em tabelas pequenas.
- metadados do TMDB (títulos, pôster, temporadas, ordens alternativas): uma
  vez por obra em media / media_localized / media_seasons /
  media_episode_groups, compartilhados pelos jobs do mesmo tmdb_id.
- configuração: chave/valor em settings e JSON opaco onde é forma livre
  (presets, merge avançado); as listas da configuração em tabelas —
  language_markers (por idioma), subtitle_markers e search_rules.
- events: append-only, um INSERT por evento — nada de regravar tudo.
- WAL + lock: seguro para o event loop e para a thread do merge.
- Migracao automatica: se existir jobs.json (formato antigo), importa e
  renomeia para jobs.json.bak.
"""
import hashlib
import json
import sqlite3
import threading
from array import array
from operator import itemgetter

import numpy as np

import config
from services import candidates as cands

MAX_EVENTS_RETURNED = 500  # o detalhe do job devolve os N eventos mais recentes

_conn: sqlite3.Connection | None = None
_lock = threading.Lock()


def init():
    global _conn
    if _conn is not None:
        return
    # garante o diretorio do banco (ex.: /data montado como volume no Docker)
    config.DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    _conn = sqlite3.connect(config.DB_FILE, check_same_thread=False)
    _conn.execute("PRAGMA journal_mode=WAL")
    _conn.execute("PRAGMA synchronous=NORMAL")
    # o SQLite vem com FKs DESLIGADAS por conexão: sem isto o ON DELETE
    # CASCADE das tabelas filhas do job não roda e remover job deixa órfãos
    _conn.execute("PRAGMA foreign_keys = ON")
    _conn.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            status TEXT NOT NULL,
            data TEXT NOT NULL
        )""")
    _conn.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL,
            ts TEXT NOT NULL,
            kind TEXT NOT NULL,
            message TEXT NOT NULL,
            data TEXT
        )""")
    _conn.execute("CREATE INDEX IF NOT EXISTS idx_events_job ON events(job_id)")
    _conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status)")
    _conn.execute("""
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
    cols = {r[1] for r in _conn.execute("PRAGMA table_info(destinations)")}
    if "media" not in cols:
        _conn.execute("ALTER TABLE destinations "
                      "ADD COLUMN media TEXT NOT NULL DEFAULT 'movie'")
    _conn.execute("""
        CREATE TABLE IF NOT EXISTS torrent_targets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT NOT NULL,
            save_path TEXT NOT NULL DEFAULT '',
            local_path TEXT NOT NULL DEFAULT '',
            is_default INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )""")
    # tabela chave/valor (senha, api key, e o que mais precisar guardar)
    _conn.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )""")
    # presets de conversao (ConvertOptions nomeadas). name UNIQUE: salvar com um
    # nome existente sobrescreve, em vez de duplicar a entrada na lista.
    _conn.execute("""
        CREATE TABLE IF NOT EXISTS convert_presets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            options TEXT NOT NULL
        )""")
    # idiomas da faixa dublada (editaveis pela UI). Os marcadores moram em
    # language_markers; as colunas markers_* (JSON) são do formato antigo —
    # lidas uma vez por _migrate_config_lists e sem uso depois.
    _conn.execute("""
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
    _conn.commit()
    _migrate_from_json()
    _seed_default_destination()
    _seed_default_torrent_target()
    _migrate_config_lists()   # antes do seed: banco novo não tem o que migrar
    _seed_languages()
    load_language_config()


# -------------------- settings (chave/valor) --------------------

def get_setting(key: str, default: str | None = None) -> str | None:
    with _lock:
        r = _conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return r[0] if r else default


def set_setting(key: str, value: str):
    with _lock:
        _conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value))
        _conn.commit()


# -------------------- regras de busca extra (idioma x variante x indexers) --------------------
# Uma linha por (idioma, regra, tracker) em search_rules. Na API e para quem
# lê, o formato continua:
#   { "<lang>": { "no_year": ["indexerId", ...], "roman": [...], "roman_no_year": [...] } }
# lang = código do idioma (pt/es/...); as chaves de variante são fixas.
# `pos` é a ordem da gravação inteira: a leitura devolve o dict na mesma ordem
# de idiomas, regras e trackers (os trackers de uma regra viram buscas nessa
# ordem — services/jobs/search.py).
_EXTRA_SEARCH_KEY = "extra_search_rules"   # formato antigo (JSON em settings)
EXTRA_SEARCH_VARIANTS = ("no_year", "roman", "roman_no_year")


def _search_rule_rows(rules) -> list[tuple[str, str, str]]:
    """{idioma: {regra: [trackers]}} -> [(idioma, regra, tracker)] na ordem.
    O que não tem a forma (idioma sem dict, regra sem lista) fica de fora."""
    out = []
    if not isinstance(rules, dict):
        return out
    for lang, variants in rules.items():
        if not isinstance(variants, dict):
            continue
        for rule, trackers in variants.items():
            if not isinstance(trackers, list):
                continue
            out.extend((str(lang), str(rule), str(t)) for t in trackers
                       if t is not None and t != "")
    return out


def _legacy_search_rules(raw: str | None) -> list[tuple[str, str, str]]:
    """settings.extra_search_rules (JSON do formato antigo) -> linhas de
    search_rules. Função pura; JSON inválido = sem regras (era o que a
    leitura antiga devolvia)."""
    if not raw:
        return []
    try:
        return _search_rule_rows(json.loads(raw))
    except (ValueError, TypeError):
        return []


def _write_search_rules(rows: list[tuple[str, str, str]]):
    """Substitui as regras (com _lock, na transação de quem chama)."""
    _conn.execute("DELETE FROM search_rules")
    _conn.executemany(
        "INSERT INTO search_rules (pos, language, rule, tracker) "
        "VALUES (?, ?, ?, ?)", [(i, *r) for i, r in enumerate(rows)])


def get_extra_search_rules() -> dict:
    with _lock:
        rows = _conn.execute("SELECT language, rule, tracker FROM search_rules "
                             "ORDER BY pos").fetchall()
    out: dict = {}
    for lang, rule, tracker in rows:
        out.setdefault(lang, {}).setdefault(rule, []).append(tracker)
    return out


def set_extra_search_rules(rules: dict):
    with _lock:
        _write_search_rules(_search_rule_rows(rules))
        _conn.commit()


# -------------------- presets de conversao --------------------
# ConvertOptions nomeadas ("QSV AV1 CRF 20"). Guardadas como JSON opaco: quem
# valida e o transcode.validate() na hora de converter, entao um preset salvo
# antes de uma mudanca de capabilities nao quebra a listagem.

def list_convert_presets() -> list[dict]:
    with _lock:
        rows = _conn.execute(
            "SELECT id, name, options FROM convert_presets "
            "ORDER BY name COLLATE NOCASE").fetchall()
    return [{"id": r[0], "name": r[1], "options": _json_or_none(r[2])} for r in rows]


def _json_or_none(raw: str) -> dict | None:
    try:
        v = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return v if isinstance(v, dict) else None


def create_convert_preset(name: str, options: dict) -> dict:
    """Cria ou sobrescreve pelo nome (salvar de novo com o mesmo nome atualiza)."""
    payload = json.dumps(options, ensure_ascii=False)
    with _lock:
        _conn.execute(
            "INSERT INTO convert_presets (name, options) VALUES (?, ?) "
            "ON CONFLICT(name) DO UPDATE SET options = excluded.options",
            (name, payload))
        _conn.commit()
        r = _conn.execute(
            "SELECT id, name, options FROM convert_presets WHERE name = ?",
            (name,)).fetchone()
    return {"id": r[0], "name": r[1], "options": _json_or_none(r[2])}


def delete_convert_preset(preset_id: int) -> bool:
    with _lock:
        cur = _conn.execute("DELETE FROM convert_presets WHERE id = ?", (preset_id,))
        _conn.commit()
        return cur.rowcount > 0


# -------------------- destinos (pastas de destino do arquivo final) --------------------

def list_destinations() -> list[dict]:
    with _lock:
        rows = _conn.execute(
            "SELECT id, label, path, media, is_default FROM destinations "
            "ORDER BY is_default DESC, label COLLATE NOCASE").fetchall()
    return [_dest_row(r) for r in rows]


def _dest_row(r) -> dict:
    return {"id": r[0], "label": r[1], "path": r[2], "media": r[3],
            "is_default": bool(r[4])}


def list_destinations_by_media(media: str) -> list[dict]:
    """Destinos de UMA mídia ('movie'/'tv') — filmes e séries têm bibliotecas
    separadas; cada fluxo só enxerga os destinos da própria mídia."""
    with _lock:
        rows = _conn.execute(
            "SELECT id, label, path, media, is_default FROM destinations "
            "WHERE media = ? ORDER BY is_default DESC, label COLLATE NOCASE",
            (media,)).fetchall()
    return [_dest_row(r) for r in rows]


def get_destination(dest_id: int) -> dict | None:
    with _lock:
        r = _conn.execute(
            "SELECT id, label, path, media, is_default FROM destinations WHERE id = ?",
            (dest_id,)).fetchone()
    return _dest_row(r) if r else None


def default_destination(media: str = "movie") -> dict | None:
    """Destino padrão DA MÍDIA (o is_default é por mídia)."""
    with _lock:
        r = _conn.execute(
            "SELECT id, label, path, media, is_default FROM destinations "
            "WHERE media = ? ORDER BY is_default DESC, id LIMIT 1",
            (media,)).fetchone()
    return _dest_row(r) if r else None


def add_destination(label: str, path: str, is_default: bool = False,
                    media: str = "movie") -> dict:
    from datetime import datetime
    with _lock:
        if is_default:
            # o padrão é POR mídia: marcar um destino de série como padrão não
            # desmarca o padrão dos filmes
            _conn.execute("UPDATE destinations SET is_default = 0 WHERE media = ?",
                          (media,))
        cur = _conn.execute(
            "INSERT INTO destinations (label, path, media, is_default, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (label, path, media, 1 if is_default else 0,
             datetime.now().isoformat(timespec="seconds")))
        _conn.commit()
        dest_id = cur.lastrowid
    return get_destination(dest_id)


def update_destination(dest_id: int, label: str, path: str, is_default: bool,
                       media: str | None = None) -> dict | None:
    with _lock:
        row = _conn.execute(
            "SELECT media FROM destinations WHERE id = ?", (dest_id,)).fetchone()
        if not row:
            return None
        media = media or row[0]
        if is_default:
            _conn.execute("UPDATE destinations SET is_default = 0 WHERE media = ?",
                          (media,))
        _conn.execute(
            "UPDATE destinations SET label = ?, path = ?, media = ?, "
            "is_default = ? WHERE id = ?",
            (label, path, media, 1 if is_default else 0, dest_id))
        _conn.commit()
    return get_destination(dest_id)


def delete_destination(dest_id: int) -> bool:
    with _lock:
        cur = _conn.execute("DELETE FROM destinations WHERE id = ?", (dest_id,))
        _conn.commit()
        return cur.rowcount > 0


def _seed_default_destination():
    """Na primeira vez, cria um destino a partir do OUTPUT_DIR do .env (se houver)."""
    with _lock:
        count = _conn.execute("SELECT COUNT(*) FROM destinations").fetchone()[0]
    if count:
        return
    output_dir = getattr(config, "OUTPUT_DIR", None)
    if output_dir:
        # as.posix() evita backslashes quando o .env foi lido no Windows
        add_destination("Padrão (OUTPUT_DIR)", output_dir.as_posix(), is_default=True)


# -------------------- destinos dos torrents (qBittorrent) --------------------

def _target_row(r) -> dict:
    return {"id": r[0], "label": r[1], "save_path": r[2], "local_path": r[3],
            "is_default": bool(r[4])}


def list_torrent_targets() -> list[dict]:
    with _lock:
        rows = _conn.execute(
            "SELECT id, label, save_path, local_path, is_default FROM torrent_targets "
            "ORDER BY is_default DESC, label COLLATE NOCASE").fetchall()
    return [_target_row(r) for r in rows]


def get_torrent_target(target_id: int) -> dict | None:
    with _lock:
        r = _conn.execute(
            "SELECT id, label, save_path, local_path, is_default FROM torrent_targets "
            "WHERE id = ?", (target_id,)).fetchone()
    return _target_row(r) if r else None


def default_torrent_target() -> dict | None:
    with _lock:
        r = _conn.execute(
            "SELECT id, label, save_path, local_path, is_default FROM torrent_targets "
            "ORDER BY is_default DESC, id LIMIT 1").fetchone()
    return _target_row(r) if r else None


def add_torrent_target(label: str, save_path: str, local_path: str,
                       is_default: bool = False) -> dict:
    from datetime import datetime
    with _lock:
        if is_default:
            _conn.execute("UPDATE torrent_targets SET is_default = 0")
        cur = _conn.execute(
            "INSERT INTO torrent_targets (label, save_path, local_path, is_default, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (label, save_path, local_path, 1 if is_default else 0,
             datetime.now().isoformat(timespec="seconds")))
        _conn.commit()
        target_id = cur.lastrowid
    return get_torrent_target(target_id)


def update_torrent_target(target_id: int, label: str, save_path: str,
                          local_path: str, is_default: bool) -> dict | None:
    with _lock:
        if not _conn.execute("SELECT 1 FROM torrent_targets WHERE id = ?",
                             (target_id,)).fetchone():
            return None
        if is_default:
            _conn.execute("UPDATE torrent_targets SET is_default = 0")
        _conn.execute(
            "UPDATE torrent_targets SET label = ?, save_path = ?, local_path = ?, "
            "is_default = ? WHERE id = ?",
            (label, save_path, local_path, 1 if is_default else 0, target_id))
        _conn.commit()
    return get_torrent_target(target_id)


def delete_torrent_target(target_id: int) -> bool:
    with _lock:
        cur = _conn.execute("DELETE FROM torrent_targets WHERE id = ?", (target_id,))
        _conn.commit()
        return cur.rowcount > 0


def _seed_default_torrent_target():
    """Na primeira vez, cria um target a partir do QBIT_SAVE_PATH/QBIT_PATH_MAP do .env."""
    with _lock:
        count = _conn.execute("SELECT COUNT(*) FROM torrent_targets").fetchone()[0]
    if count:
        return
    save_path = getattr(config, "QBIT_SAVE_PATH", "") or ""
    path_map = getattr(config, "QBIT_PATH_MAP", []) or []
    # se havia QBIT_PATH_MAP, usa o primeiro par como save->local do target
    local_path = ""
    if path_map:
        src, dst = path_map[0]
        if not save_path:
            save_path = src
        local_path = dst
    if save_path or local_path:
        add_torrent_target("Padrão (.env)", save_path, local_path, is_default=True)


# -------------------- idiomas da faixa dublada (editaveis) --------------------
# Os marcadores de cada idioma (fortes e fracos) e os de legenda são linhas,
# na ordem em que a tela os mostra. O seletor só pergunta "algum casa?"
# (selector.marker_strength, any()) e a regex de legenda é uma alternância
# entre \b — a ordem não muda o resultado hoje, mas a lista volta para a
# tela como foi editada.

def _create_config_list_tables():
    _conn.execute("""
        CREATE TABLE IF NOT EXISTS language_markers (
            language TEXT NOT NULL REFERENCES languages(code) ON DELETE CASCADE,
            strength TEXT NOT NULL CHECK (strength IN ('strong', 'weak')),
            pos INTEGER NOT NULL,
            marker TEXT NOT NULL,
            PRIMARY KEY (language, strength, pos)
        )""")
    _conn.execute("""
        CREATE TABLE IF NOT EXISTS subtitle_markers (
            pos INTEGER PRIMARY KEY,
            marker TEXT NOT NULL
        )""")
    # sem FK para languages: as regras de um idioma sobrevivem a regravar a
    # tabela de idiomas (save_languages apaga e reinsere todos), como no JSON
    _conn.execute("""
        CREATE TABLE IF NOT EXISTS search_rules (
            pos INTEGER PRIMARY KEY,
            language TEXT NOT NULL,
            rule TEXT NOT NULL,
            tracker TEXT NOT NULL
        )""")
    _conn.execute("CREATE INDEX IF NOT EXISTS idx_search_rules_lang "
                  "ON search_rules(language)")


_STRENGTHS = (("strong", "markers_strong"), ("weak", "markers_weak"))
# chave em settings que marca a migração das listas (JSON -> tabelas) feita
_CONFIG_LISTS_FLAG = "config_lists_in_tables"


def _legacy_markers(raw) -> list[str]:
    """Lista JSON do formato antigo (languages.markers_*, settings.
    subtitle_markers) -> marcadores na ordem. Função pura; inválido = []
    (era o que a leitura antiga devolvia)."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "[]")
        except (ValueError, TypeError):
            return []
    return [str(m) for m in raw] if isinstance(raw, list) else []


def _write_language_markers(code: str, lg: dict):
    """Marcadores de UM idioma (com _lock, na transação de quem chama)."""
    _conn.executemany(
        "INSERT INTO language_markers (language, strength, pos, marker) "
        "VALUES (?, ?, ?, ?)",
        [(code, strength, i, m) for strength, key in _STRENGTHS
         for i, m in enumerate(lg.get(key) or [])])


def _write_subtitle_markers(markers: list[str]):
    _conn.execute("DELETE FROM subtitle_markers")
    _conn.executemany("INSERT INTO subtitle_markers (pos, marker) VALUES (?, ?)",
                      list(enumerate(markers)))


def _migrate_config_lists():
    """Banco anterior às tabelas: lê UMA vez os JSON antigos (markers_* de
    languages, settings.subtitle_markers, settings.extra_search_rules) e grava
    as linhas. Tudo numa transação, com a marca junto: rodar de novo (init()
    outra vez, restart) não duplica nem volta a ler o JSON — que fica no banco
    sem uso. Banco novo (sem a chave de legenda): os marcadores padrão."""
    with _lock:
        if _conn.execute("SELECT 1 FROM settings WHERE key = ?",
                         (_CONFIG_LISTS_FLAG,)).fetchone():
            return
        try:
            langs = _conn.execute(
                "SELECT code, markers_strong, markers_weak FROM languages"
            ).fetchall()
            _conn.execute("DELETE FROM language_markers")
            for code, strong, weak in langs:
                _write_language_markers(code, {
                    "markers_strong": _legacy_markers(strong),
                    "markers_weak": _legacy_markers(weak)})
            r = _conn.execute("SELECT value FROM settings WHERE key = ?",
                              ("subtitle_markers",)).fetchone()
            # chave vazia/ausente: a leitura antiga caía nos padrões
            _write_subtitle_markers(
                _legacy_markers(r[0]) if r and r[0]
                else list(config._DEFAULT_SUBTITLE_MARKERS))
            r = _conn.execute("SELECT value FROM settings WHERE key = ?",
                              (_EXTRA_SEARCH_KEY,)).fetchone()
            _write_search_rules(_legacy_search_rules(r[0] if r else None))
            _conn.execute("INSERT INTO settings (key, value) VALUES (?, '1')",
                          (_CONFIG_LISTS_FLAG,))
            _conn.commit()
        except BaseException:
            _conn.rollback()
            raise


def _seed_languages():
    """Na primeira execução, popula a tabela com os idiomas padrão do config."""
    with _lock:
        count = _conn.execute("SELECT COUNT(*) FROM languages").fetchone()[0]
        if count:
            return
        for i, (code, info) in enumerate(config._DEFAULT_LANGUAGES.items()):
            _conn.execute(
                "INSERT INTO languages (code, label, tmdb, sort_order) "
                "VALUES (?, ?, ?, ?)", (code, info["label"], info["tmdb"], i))
            _write_language_markers(code, info)
        _conn.commit()


def list_languages() -> list[dict]:
    with _lock:
        rows = _conn.execute(
            "SELECT code, label, tmdb, sort_order FROM languages "
            "ORDER BY sort_order, code").fetchall()
        marks = _conn.execute(
            "SELECT language, strength, marker FROM language_markers "
            "ORDER BY language, strength, pos").fetchall()
    by_lang: dict[str, dict[str, list]] = {}
    for code, strength, marker in marks:
        by_lang.setdefault(code, {}).setdefault(strength, []).append(marker)
    out = []
    for code, label, tmdb, order in rows:
        m = by_lang.get(code, {})
        out.append({"code": code, "label": label, "tmdb": tmdb,
                    **{key: list(m.get(strength, ())) for strength, key in _STRENGTHS},
                    "sort_order": order})
    return out


def get_subtitle_markers() -> list[str]:
    with _lock:
        rows = _conn.execute(
            "SELECT marker FROM subtitle_markers ORDER BY pos").fetchall()
    return [r[0] for r in rows]


def load_language_config():
    """Lê idiomas + marcadores de legenda do banco e instala em config (runtime)."""
    langs = {}
    for row in list_languages():
        langs[row["code"]] = {
            "label": row["label"], "tmdb": row["tmdb"],
            "markers_strong": row["markers_strong"],
            "markers_weak": row["markers_weak"],
        }
    config.install_language_config(langs, get_subtitle_markers())


def save_languages(languages: list[dict], subtitle_markers: list[str]):
    """Substitui TODA a configuração de idiomas + marcadores e recarrega o runtime."""
    with _lock:
        try:
            # o CASCADE já leva os marcadores; o DELETE explícito não depende
            # do PRAGMA foreign_keys
            _conn.execute("DELETE FROM language_markers")
            _conn.execute("DELETE FROM languages")
            for i, lg in enumerate(languages):
                _conn.execute(
                    "INSERT INTO languages (code, label, tmdb, sort_order) "
                    "VALUES (?, ?, ?, ?)", (lg["code"], lg["label"], lg["tmdb"], i))
                _write_language_markers(lg["code"], lg)
            _write_subtitle_markers(list(subtitle_markers))
            _conn.commit()
        except BaseException:
            _conn.rollback()
            raise
    load_language_config()


# -------------------- candidatos de busca (tabelas do job) --------------------
# Formato em memória e semântica: services/candidates.py. Uma linha por
# candidato (o rastro de cada consulta, a lista escolhível, as reservas e o
# ativo são colunas da MESMA linha) e a posição de cada um no ranking de cada
# episódio (série) em candidate_episodes.

def _create_candidate_tables():
    _conn.execute("""
        CREATE TABLE IF NOT EXISTS search_queries (
            job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
            n INTEGER NOT NULL,
            role TEXT NOT NULL,
            query TEXT NOT NULL,
            ts TEXT,
            results INTEGER,
            PRIMARY KEY (job_id, n)
        )""")
    _conn.execute("""
        CREATE TABLE IF NOT EXISTS search_candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
            cid TEXT NOT NULL,
            query_n INTEGER,
            role TEXT NOT NULL,
            title TEXT NOT NULL,
            tracker TEXT,
            seeders INTEGER,
            size INTEGER,
            edition TEXT,
            quality TEXT,
            coverage_label TEXT,
            score REAL,
            tier INTEGER,
            year_match INTEGER,
            rejected TEXT,
            chosen INTEGER,
            magnet TEXT,
            link TEXT,
            infohash TEXT,
            files INTEGER,
            trace_rank INTEGER,
            rank INTEGER,
            fallback INTEGER,
            active INTEGER NOT NULL DEFAULT 0,
            UNIQUE (job_id, cid),
            FOREIGN KEY (job_id, query_n)
                REFERENCES search_queries(job_id, n) ON DELETE CASCADE
        )""")
    _conn.execute("CREATE INDEX IF NOT EXISTS idx_search_candidates_query "
                  "ON search_candidates(job_id, query_n)")
    _conn.execute("""
        CREATE TABLE IF NOT EXISTS candidate_episodes (
            candidate_id INTEGER NOT NULL
                REFERENCES search_candidates(id) ON DELETE CASCADE,
            ep_key TEXT NOT NULL,
            rank INTEGER NOT NULL,
            PRIMARY KEY (candidate_id, ep_key)
        )""")


# colunas de search_candidates <-> chaves do candidato em memória
_CAND_COLS = (("cid", "id"), ("query_n", "query"), ("role", "role"),
              ("title", "title"), ("tracker", "tracker"), ("seeders", "seeders"),
              ("size", "size"), ("edition", "edition"), ("quality", "quality"),
              ("coverage_label", "coverage"), ("score", "score"), ("tier", "tier"),
              ("year_match", "year_match"), ("rejected", "rejected"),
              ("chosen", "chosen"), ("magnet", "magnet"), ("link", "link"),
              ("infohash", "infohash"), ("files", "files"),
              ("trace_rank", "trace"), ("rank", "rank"), ("fallback", "fallback"),
              ("active", "active"))
_BOOL_KEYS = {"year_match", "chosen", "active"}

# seções do job que moram em tabela própria (não vão para jobs.data)
_SECTIONS = ("search_queries", "search_candidates")
# chaves do formato antigo: hoje são derivadas/normalizadas — nunca gravadas
_LEGACY_KEYS = ("search", "fallbacks", "current", "video_torrent",
                "audio_torrent", "search_tv")

# hash das últimas linhas GRAVADAS das seções de cada job: o upsert só
# reescreve as linhas quando elas mudaram (a maioria das gravações do job é
# progresso/estado — candidatos mudam na busca, na escolha e nas trocas).
#   {job_id: {"candidates": hash da seção,
#             "episodes": {ep_key: hash da linha},
#             "torrents": {n: (hash da linha, hash de cobertura+arquivos)},
#             "edls": {ep_key | "filme": hash da EDL},
#             "scalars": {tabela filha | "media" | "seasons" | "groups": hash}}}
# Candidatos mudam em bloco e a seção inteira é reescrita; episódios,
# torrents e EDLs mudam um de cada vez (o merge conclui UM episódio, o
# watchdog atualiza o progresso de UM torrent, a revisão decide a EDL de UM
# episódio), então o hash é por linha e só a linha que mudou é regravada.
# As tabelas pequenas dos escalares (progresso do filme, pedido...) e os
# metadados compartilhados têm um hash cada. Sem entrada para a seção
# (processo novo, ou o job veio do formato antigo), o próximo upsert
# reescreve a seção inteira.
_section_hash: dict[str, dict] = {}


def _section_rows(job: dict) -> tuple[list, list]:
    """As linhas das tabelas de candidatos, como tuplas prontas para o INSERT.
    Montadas FORA do lock e antes de qualquer escrita: dict mexido por outra
    thread no meio (RuntimeError) não deixa gravação pela metade."""
    qs = [(q["n"], q["role"], q["query"], q.get("ts"), q.get("results"))
          for q in job.get("search_queries") or []]
    cs = []
    for c in job.get("search_candidates") or []:
        vals = tuple(
            (1 if c.get(k) else 0) if k == "active"
            else (None if c.get(k) is None else int(c[k])) if k in _BOOL_KEYS
            else c.get(k)
            for _, k in _CAND_COLS)
        cs.append((vals, tuple((c.get("episodes") or {}).items())))
    return qs, cs


def _rows_digest(rows: tuple[list, list]) -> str:
    raw = json.dumps(rows, ensure_ascii=False)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _write_sections(job_id: str, rows: tuple[list, list]):
    """Reescreve as linhas de candidatos do job (chamado com _lock, dentro da
    transação do upsert)."""
    qs, cs = rows
    # candidatos primeiro (o CASCADE leva candidate_episodes junto); as
    # consultas depois, já sem filhos apontando para elas
    _conn.execute("DELETE FROM search_candidates WHERE job_id = ?", (job_id,))
    _conn.execute("DELETE FROM search_queries WHERE job_id = ?", (job_id,))
    _conn.executemany(
        "INSERT INTO search_queries (job_id, n, role, query, ts, results) "
        "VALUES (?, ?, ?, ?, ?, ?)", [(job_id,) + q for q in qs])
    cols = ", ".join(c for c, _ in _CAND_COLS)
    ph = ", ".join("?" for _ in range(len(_CAND_COLS) + 1))
    for vals, eps in cs:
        cur = _conn.execute(
            f"INSERT INTO search_candidates (job_id, {cols}) VALUES ({ph})",
            (job_id,) + vals)
        if eps:
            _conn.executemany(
                "INSERT INTO candidate_episodes (candidate_id, ep_key, rank) "
                "VALUES (?, ?, ?)", [(cur.lastrowid, k, pos) for k, pos in eps])


def _load_candidates(job_ids: list[str]) -> dict[str, dict]:
    """{job_id: {search_queries, search_candidates}} lidos das tabelas (com
    _lock já tomado). Em lotes, para a lista de jobs não virar N consultas."""
    out = {jid: {"search_queries": [], "search_candidates": []} for jid in job_ids}
    cols = ", ".join(c for c, _ in _CAND_COLS)
    for i in range(0, len(job_ids), 500):
        chunk = job_ids[i:i + 500]
        ph = ",".join("?" for _ in chunk)
        for jid, n, role, query, ts, results in _conn.execute(
                f"SELECT job_id, n, role, query, ts, results FROM search_queries "
                f"WHERE job_id IN ({ph}) ORDER BY job_id, n", chunk):
            out[jid]["search_queries"].append(
                {"n": n, "role": role, "query": query, "ts": ts, "results": results})
        by_rowid: dict[int, dict] = {}
        for r in _conn.execute(
                f"SELECT id, job_id, {cols} FROM search_candidates "
                f"WHERE job_id IN ({ph}) ORDER BY id", chunk):
            kw = {}
            for (_, k), v in zip(_CAND_COLS, r[2:]):
                kw[k] = bool(v) if (k in _BOOL_KEYS and v is not None) else v
            c = cands.new(kw["role"], kw["id"], kw, **{
                k: kw[k] for k in ("query", "trace", "rank", "fallback")})
            c["active"] = bool(kw["active"])
            by_rowid[r[0]] = c
            out[r[1]]["search_candidates"].append(c)
        for cand_id, key, pos in _conn.execute(
                f"SELECT e.candidate_id, e.ep_key, e.rank FROM candidate_episodes e "
                f"JOIN search_candidates c ON c.id = e.candidate_id "
                f"WHERE c.job_id IN ({ph}) ORDER BY e.rowid", chunk):
            by_rowid[cand_id]["episodes"][key] = pos
    return out


def _is_legacy(doc: dict) -> bool:
    return any(k in doc for k in _LEGACY_KEYS)


def _legacy_candidates(doc: dict) -> dict:
    """Documento no formato antigo -> formato normalizado (função pura).

    - filme: `search` vira a lista escolhível; `fallbacks` (todas também em
      `search`) vira a posição de reserva; `current` vira o candidato ativo;
      `video_torrent`/`audio_torrent` (cópia do escolhido) só servem para
      achar o ativo de jobs anteriores ao `current` — ou viram um candidato
      sem link, para o detalhe continuar mostrando o que foi baixado.
    - série: `search_tv` (o mesmo pack repetido em cada episódio) vira um
      candidato único com a posição em cada episódio; o id do primeiro vale,
      e os ids antigos dos outros episódios resolvem pela posição
      (candidates.find).
    - gates de série agora derivados: o payload gravado sai.
    """
    doc = dict(doc)
    search = doc.pop("search", None) or {}
    fallbacks = doc.pop("fallbacks", None) or {}
    current = doc.pop("current", None) or {}
    chosen = {"video": doc.pop("video_torrent", None),
              "audio": doc.pop("audio_torrent", None)}
    search_tv = doc.pop("search_tv", None) or {}
    job = {"search_queries": list(doc.get("search_queries") or []),
           "search_candidates": list(doc.get("search_candidates") or [])}

    for role in ("audio", "video"):
        for i, c in enumerate(search.get(role) or []):
            cid = c.get("id") or f"{cands.ROLE_LETTER[role]}{i}"
            if cands.find(job, cid, role) is None:
                cands.rows(job).append(cands.new(role, cid, c, rank=i))
        reservas = [cands.ensure(job, role, c) for c in fallbacks.get(role) or []]
        cands.set_fallbacks(job, role, reservas)
        cur = current.get(role)
        if not cur and chosen[role]:
            # jobs antigos (sem "current"): acha o candidato pelo título escolhido
            title = chosen[role].get("title")
            cur = next((c for c in cands.selectable(job, role)
                        if c["title"] == title), None) or chosen[role]
        if cur:
            cands.set_active(job, role, cands.ensure(job, role, cur))

    for role in ("original", "dubbed"):
        if search_tv.get(role):
            cands.set_series(job, role, search_tv[role])

    gate = doc.get("awaiting")
    if (doc.get("media_type") == "tv" and isinstance(gate, dict)
            and gate.get("reason") in ("manual_pick", "incompatible_torrents")):
        doc["awaiting"] = {**gate, "payload": None}
    doc.update(job)
    return doc


# -------------------- episódios e torrents (jobs de série) --------------------
# Formato em memória (o mesmo de sempre, quem muta o dict não muda):
#   job["episodes"]  = {ep_key: {season, episode, name, air_date, runtime,
#                       state, error, output, src{original,dubbed|file},
#                       subs{papel: [caminhos]}, rel, edl}}
#   job["order_map"] = {ep_key: [s, e]} na ordem alternativa (ou None)
#   job["torrents"]  = [{n, role, tag, title, tracker, ..., coverage,
#                        coverage_shrunk, state, hash, progress{...},
#                        selected_files, content_path}]
# Vira: job_episodes (+ alt_season/alt_episode do order_map) e episode_subs;
# job_torrents (progresso em colunas) com torrent_coverage e torrent_files.
#
# O torrent do plano é uma CÓPIA do candidato escolhido (título, tracker,
# magnet...). Gravado, ele aponta para o candidato por (job_id, cid) e não
# repete os campos; os campos só vão na linha quando não há candidato igual
# (magnet/link próprio do usuário, torrent do plano fora do top do ranking,
# job sem candidatos carregados). O id da linha de search_candidates não serve
# de referência: muda a cada reescrita da seção de candidatos.
#
# A EDL do episódio mora em edls/edl_segments (ver a seção das EDLs). A
# coluna job_episodes.edl é a morada PROVISÓRIA que ela teve antes: só é lida
# (normalização legada) e zerada quando a EDL passa para as tabelas — nunca
# mais gravada. Chaves que este formato não conhece vão para `extra` (JSON),
# para nada se perder no caminho.

def _create_series_tables():
    _conn.execute("""
        CREATE TABLE IF NOT EXISTS job_episodes (
            job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
            ep_key TEXT NOT NULL,
            pos INTEGER NOT NULL,
            season INTEGER,
            episode INTEGER,
            name TEXT,
            air_date TEXT,
            runtime INTEGER,
            state TEXT,
            error TEXT,
            output TEXT,
            src_original TEXT,
            src_dubbed TEXT,
            src_file TEXT,
            rel TEXT,
            alt_season INTEGER,
            alt_episode INTEGER,
            edl TEXT,  -- legado (EDL em JSON, antes das tabelas de EDL)
            extra TEXT,
            PRIMARY KEY (job_id, ep_key)
        )""")
    _conn.execute("""
        CREATE TABLE IF NOT EXISTS episode_subs (
            job_id TEXT NOT NULL,
            ep_key TEXT NOT NULL,
            role TEXT NOT NULL,
            pos INTEGER NOT NULL,
            path TEXT NOT NULL,
            PRIMARY KEY (job_id, ep_key, role, pos),
            FOREIGN KEY (job_id, ep_key)
                REFERENCES job_episodes(job_id, ep_key) ON DELETE CASCADE
        )""")
    # FK do candidato ADIADA para o commit: a seção de candidatos é reescrita
    # com DELETE + INSERT dentro da mesma transação, e o torrent que aponta
    # para (job_id, cid) só precisa achá-lo de novo no fim
    _conn.execute("""
        CREATE TABLE IF NOT EXISTS job_torrents (
            job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
            n INTEGER NOT NULL,
            role TEXT NOT NULL,
            tag TEXT,
            cid TEXT,
            title TEXT,
            tracker TEXT,
            seeders INTEGER,
            size INTEGER,
            quality TEXT,
            coverage_label TEXT,
            magnet TEXT,
            link TEXT,
            infohash TEXT,
            files_count INTEGER,
            state TEXT,
            hash TEXT,
            content_path TEXT,
            files_selected INTEGER NOT NULL DEFAULT 0,
            progress_pct REAL,
            progress_speed INTEGER,
            progress_eta INTEGER,
            progress_state TEXT,
            progress_seeds INTEGER,
            progress_name TEXT,
            progress_size INTEGER,
            progress_downloaded INTEGER,
            extra TEXT,
            PRIMARY KEY (job_id, n),
            FOREIGN KEY (job_id, cid) REFERENCES search_candidates(job_id, cid)
                DEFERRABLE INITIALLY DEFERRED
        )""")
    _conn.execute("CREATE INDEX IF NOT EXISTS idx_job_torrents_cand "
                  "ON job_torrents(job_id, cid)")
    # pos NULL = o episódio saiu da cobertura (só está ali pela flag shrunk:
    # o pack não tinha o arquivo — ver pipeline._ensure_file_selection)
    _conn.execute("""
        CREATE TABLE IF NOT EXISTS torrent_coverage (
            job_id TEXT NOT NULL,
            n INTEGER NOT NULL,
            ep_key TEXT NOT NULL,
            pos INTEGER,
            shrunk INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (job_id, n, ep_key),
            FOREIGN KEY (job_id, n)
                REFERENCES job_torrents(job_id, n) ON DELETE CASCADE
        )""")
    _conn.execute("""
        CREATE TABLE IF NOT EXISTS torrent_files (
            job_id TEXT NOT NULL,
            n INTEGER NOT NULL,
            pos INTEGER NOT NULL,
            path TEXT NOT NULL,
            PRIMARY KEY (job_id, n, pos),
            FOREIGN KEY (job_id, n)
                REFERENCES job_torrents(job_id, n) ON DELETE CASCADE
        )""")


# chaves do job de série que moram em tabela (ou são derivadas: `report` sai
# do estado dos episódios — merge_runner.report_view) e nunca vão em jobs.data
_SERIES_KEYS = ("episodes", "order_map", "torrents", "report")

_EP_PLAIN = ("season", "episode", "name", "air_date", "runtime", "state",
             "error", "output")
_SRC_KEYS = ("original", "dubbed", "file")   # file: recompressão em lote
_EP_KNOWN = set(_EP_PLAIN) | {"src", "subs", "rel", "edl"}
_EP_COLS = ("pos", *_EP_PLAIN, "src_original", "src_dubbed", "src_file", "rel",
            "alt_season", "alt_episode", "extra")

# (chave do torrent, chave do candidato) — os campos copiados do candidato
_TOR_DISPLAY = (("title", "title"), ("tracker", "tracker"),
                ("seeders", "seeders"), ("size", "size"),
                ("quality", "quality"), ("coverage_label", "coverage"),
                ("magnet", "magnet"), ("link", "link"),
                ("infohash", "infohash"), ("files_count", "files"))
_PROGRESS_KEYS = ("pct", "speed", "eta", "state", "seeds", "name", "size",
                  "downloaded")
_TOR_KNOWN = ({"n", "role", "tag", "coverage", "coverage_shrunk", "state",
               "hash", "progress", "selected_files", "content_path"}
              | {k for k, _ in _TOR_DISPLAY})
_TOR_COLS = ("role", "tag", "cid", *(k for k, _ in _TOR_DISPLAY), "state",
             "hash", "content_path", "files_selected",
             *(f"progress_{k}" for k in _PROGRESS_KEYS), "extra")


def _json(v) -> str | None:
    return None if v is None else json.dumps(v, ensure_ascii=False)


def _digest(*parts) -> str:
    """Hash de partes já prontas (str entra crua, sem serializar de novo)."""
    h = hashlib.sha1()
    for p in parts:
        h.update(p.encode("utf-8") if isinstance(p, str)
                 else json.dumps(p, ensure_ascii=False).encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()


def _episode_row(pos: int, ep: dict, alt) -> tuple[tuple, tuple]:
    """Episódio em memória -> (linha de job_episodes, linhas de episode_subs).
    A EDL não entra: é seção própria (edls)."""
    src = ep.get("src") or {}
    extra = {k: v for k, v in ep.items() if k not in _EP_KNOWN}
    src_extra = {k: v for k, v in src.items() if k not in _SRC_KEYS}
    if src_extra:
        extra["_src"] = src_extra
    alt_s, alt_e = (alt[0], alt[1]) if alt else (None, None)
    row = (pos, *(ep.get(k) for k in _EP_PLAIN), *(src.get(k) for k in _SRC_KEYS),
           ep.get("rel"), alt_s, alt_e, _json(extra or None))
    subs = tuple((role, i, p) for role, paths in (ep.get("subs") or {}).items()
                 for i, p in enumerate(paths or []))
    return row, subs


# posições em _EP_COLS (a listagem de séries decodifica milhares de linhas:
# acesso por índice em vez de montar um dict por linha)
_EP_N = len(_EP_PLAIN)
_EP_SRC0 = 1 + _EP_N
_EP_REL, _EP_ALT_S, _EP_ALT_E, _EP_EXTRA = (_EP_COLS.index(c) for c in (
    "rel", "alt_season", "alt_episode", "extra"))


def _episode_from_row(row: tuple, subs) -> tuple[dict, list | None]:
    """Linha de job_episodes (+ legendas) -> (episódio, [s, e] alternativo)."""
    extra = json.loads(row[_EP_EXTRA]) if row[_EP_EXTRA] else None
    src = {k: v for k, v in zip(_SRC_KEYS, row[_EP_SRC0:_EP_SRC0 + 3])
           if v is not None}
    if extra and "_src" in extra:
        src.update(extra.pop("_src") or {})
    ep = dict(zip(_EP_PLAIN, row[1:_EP_SRC0]))
    ep["src"] = src
    for role, _pos, path in subs or ():
        ep.setdefault("subs", {}).setdefault(role, []).append(path)
    if row[_EP_REL] is not None:
        ep["rel"] = row[_EP_REL]
    if extra:
        ep.update(extra)
    alt = ([row[_EP_ALT_S], row[_EP_ALT_E]]
           if row[_EP_ALT_S] is not None else None)
    return ep, alt


def _episode_items(job: dict) -> dict[str, tuple]:
    """{ep_key: (linha, legendas, hash)} do job — montado fora do lock. A
    EDL tem hash próprio (_edl_items): mudar o estado do episódio não
    serializa nem regrava a EDL dele."""
    om = job.get("order_map") or {}
    out = {}
    for pos, (key, ep) in enumerate((job.get("episodes") or {}).items()):
        row, subs = _episode_row(pos, ep, om.get(key))
        out[key] = (row, subs, _digest(row, subs))
    return out


def _candidate_index(job: dict) -> dict | None:
    """(papel, campos copiados) -> cid dos candidatos do job; None quando o
    dict não tem os candidatos (aí o torrent grava os próprios campos)."""
    cands = job.get("search_candidates")
    if cands is None:
        return None
    idx: dict = {}
    for c in cands:
        idx.setdefault((c["role"], tuple(c.get(ck) for _, ck in _TOR_DISPLAY)),
                       c["id"])
    return idx


def _torrent_row(t: dict, cand_idx: dict | None) -> tuple[tuple, tuple]:
    """Torrent em memória -> (linha de job_torrents, (cobertura, arquivos))."""
    display = tuple(t.get(k) for k, _ in _TOR_DISPLAY)
    cid = (cand_idx or {}).get((t.get("role"), display))
    if cid is not None:
        display = (None,) * len(_TOR_DISPLAY)   # vêm do candidato na leitura
    prog = t.get("progress")
    if isinstance(prog, dict):
        pvals = tuple(prog.get(k) for k in _PROGRESS_KEYS)
    else:  # None, ou o número solto de jobs muito antigos
        pvals = (prog,) + (None,) * (len(_PROGRESS_KEYS) - 1)
    sel = t.get("selected_files")
    extra = {k: v for k, v in t.items() if k not in _TOR_KNOWN}
    row = (t.get("role"), t.get("tag"), cid, *display, t.get("state"),
           t.get("hash"), t.get("content_path"), 0 if sel is None else 1,
           *pvals, _json(extra or None))
    shrunk = set(t.get("coverage_shrunk") or [])
    cov, seen = [], set()
    for i, k in enumerate(t.get("coverage") or []):
        if k not in seen:
            seen.add(k)
            cov.append((k, i, 1 if k in shrunk else 0))
    cov += [(k, None, 1) for k in sorted(shrunk - seen)]
    return row, (tuple(cov), tuple(enumerate(sel or [])))


def _torrent_from_row(n: int, row: tuple, kids: tuple,
                      cand: tuple | None) -> dict:
    """Linha de job_torrents (+ filhos, + campos do candidato) -> torrent."""
    v = dict(zip(_TOR_COLS, row))
    cov, files = kids
    t = {"n": n, "role": v["role"], "tag": v["tag"]}
    inherit = v["cid"] is not None and cand is not None
    for (k, _), cv in zip(_TOR_DISPLAY, cand if inherit else
                          (v[k] for k, _ in _TOR_DISPLAY)):
        t[k] = cv
    t["coverage"] = [k for k, pos, _ in sorted((c for c in cov if c[1] is not None),
                                               key=lambda c: c[1])]
    shrunk = sorted(k for k, _, sh in cov if sh)
    if shrunk:
        t["coverage_shrunk"] = shrunk
    t["state"], t["hash"] = v["state"], v["hash"]
    pvals = [v[f"progress_{k}"] for k in _PROGRESS_KEYS]
    t["progress"] = (None if all(p is None for p in pvals)
                     else dict(zip(_PROGRESS_KEYS, pvals)))
    t["selected_files"] = [p for _, p in files] if v["files_selected"] else None
    t["content_path"] = v["content_path"]
    if v["extra"]:
        t.update(json.loads(v["extra"]))
    return t


def _torrent_items(job: dict) -> dict[int, tuple]:
    """{n: (linha, filhos, hash da linha, hash dos filhos)} — fora do lock.
    O hash da linha inclui o progresso: o watchdog que anda 1% num torrent
    regrava SÓ a linha dele (nem os episódios, nem a cobertura)."""
    idx = _candidate_index(job)
    out = {}
    for t in job.get("torrents") or []:
        row, kids = _torrent_row(t, idx)
        out[t["n"]] = (row, kids, _digest(row), _digest(kids))
    return out


def _write_episodes(job_id: str, items: dict, cached: dict | None):
    """Grava os episódios que mudaram desde a última gravação (todos, sem
    cache). Com _lock, dentro da transação do upsert.

    Sem cache, apaga só os episódios que saíram e regrava os outros por
    upsert, em vez de apagar tudo: o UPDATE não toca a coluna legada `edl`,
    que precisa sobreviver até a EDL dela ir para as tabelas (_write_edls)."""
    if cached is None:
        stored = [r[0] for r in _conn.execute(
            "SELECT ep_key FROM job_episodes WHERE job_id = ?", (job_id,))]
        gone = [(job_id, k) for k in stored if k not in items]
        _conn.execute("DELETE FROM episode_subs WHERE job_id = ?", (job_id,))
    else:
        gone = [(job_id, k) for k in cached if k not in items]
    if gone:
        _conn.executemany(
            "DELETE FROM job_episodes WHERE job_id = ? AND ep_key = ?", gone)
    changed = (list(items) if cached is None
               else [k for k, it in items.items() if cached.get(k) != it[2]])
    if not changed:
        return
    cols = ", ".join(_EP_COLS)
    ph = ", ".join("?" for _ in range(len(_EP_COLS) + 2))
    upd = ", ".join(f"{c} = excluded.{c}" for c in _EP_COLS)
    _conn.executemany(
        f"INSERT INTO job_episodes (job_id, ep_key, {cols}) VALUES ({ph}) "
        f"ON CONFLICT(job_id, ep_key) DO UPDATE SET {upd}",
        [(job_id, k) + items[k][0] for k in changed])
    if cached is not None:
        _conn.executemany(
            "DELETE FROM episode_subs WHERE job_id = ? AND ep_key = ?",
            [(job_id, k) for k in changed])
    _conn.executemany(
        "INSERT INTO episode_subs (job_id, ep_key, role, pos, path) "
        "VALUES (?, ?, ?, ?, ?)",
        [(job_id, k) + s for k in changed for s in items[k][1]])


def _write_torrents(job_id: str, items: dict, cached: dict | None):
    """Grava os torrents que mudaram: a linha (estado, hash, progresso) e os
    filhos (cobertura, arquivos selecionados) têm hashes separados. Upsert
    com ON CONFLICT DO UPDATE — INSERT OR REPLACE apagaria a linha e, pelo
    CASCADE, os filhos junto."""
    if cached is None:
        _conn.execute("DELETE FROM job_torrents WHERE job_id = ?", (job_id,))
        rows, kids = list(items), list(items)
    else:
        gone = [(job_id, n) for n in cached if n not in items]
        if gone:
            _conn.executemany(
                "DELETE FROM job_torrents WHERE job_id = ? AND n = ?", gone)
        old = {n: cached.get(n, (None, None)) for n in items}
        rows = [n for n, it in items.items() if old[n][0] != it[2]]
        kids = [n for n, it in items.items() if old[n][1] != it[3]]
    if rows:
        cols = ", ".join(_TOR_COLS)
        ph = ", ".join("?" for _ in range(len(_TOR_COLS) + 2))
        upd = ", ".join(f"{c} = excluded.{c}" for c in _TOR_COLS)
        _conn.executemany(
            f"INSERT INTO job_torrents (job_id, n, {cols}) VALUES ({ph}) "
            f"ON CONFLICT(job_id, n) DO UPDATE SET {upd}",
            [(job_id, n) + items[n][0] for n in rows])
    if not kids:
        return
    if cached is not None:
        keys = [(job_id, n) for n in kids]
        _conn.executemany(
            "DELETE FROM torrent_coverage WHERE job_id = ? AND n = ?", keys)
        _conn.executemany(
            "DELETE FROM torrent_files WHERE job_id = ? AND n = ?", keys)
    _conn.executemany(
        "INSERT INTO torrent_coverage (job_id, n, ep_key, pos, shrunk) "
        "VALUES (?, ?, ?, ?, ?)",
        [(job_id, n) + c for n in kids for c in items[n][1][0]])
    _conn.executemany(
        "INSERT INTO torrent_files (job_id, n, pos, path) VALUES (?, ?, ?, ?)",
        [(job_id, n) + f for n in kids for f in items[n][1][1]])


def _in_chunks(job_ids: list[str]):
    for i in range(0, len(job_ids), 500):
        chunk = job_ids[i:i + 500]
        yield chunk, ",".join("?" for _ in chunk)


def _load_episodes(job_ids: list[str], legacy_edl: bool = False
                   ) -> dict[str, tuple[dict, dict | None, dict]]:
    """{job_id: (episodes, order_map, EDLs da coluna legada)} lidos das
    tabelas (com _lock). A coluna legada `edl` só é lida com `legacy_edl`
    (quem carrega as EDLs): a listagem não paga o JSON dela."""
    out: dict[str, tuple[dict, dict | None, dict]] = {}
    cols = ", ".join(_EP_COLS) + (", edl" if legacy_edl else ", NULL")
    for chunk, ph in _in_chunks(job_ids):
        subs: dict[tuple, list] = {}
        for jid, key, role, pos, path in _conn.execute(
                f"SELECT job_id, ep_key, role, pos, path FROM episode_subs "
                f"WHERE job_id IN ({ph}) ORDER BY job_id, ep_key, rowid", chunk):
            subs.setdefault((jid, key), []).append((role, pos, path))
        cur_id = None
        for r in _conn.execute(
                f"SELECT job_id, ep_key, {cols} FROM job_episodes "
                f"WHERE job_id IN ({ph}) ORDER BY job_id, pos", chunk):
            if r[0] != cur_id:        # linhas vêm agrupadas por job
                cur_id = r[0]
                eps, om, old = out.setdefault(cur_id, ({}, {}, {}))
            ep, alt = _episode_from_row(r[2:-1], subs.get((r[0], r[1])))
            eps[r[1]] = ep
            if alt:
                om[r[1]] = alt
            if r[-1] is not None:
                old[r[1]] = json.loads(r[-1])
    return {jid: (eps, om or None, old) for jid, (eps, om, old) in out.items()}


# colunas de search_candidates com os campos que o torrent copia
_CAND_COL_OF = {k: c for c, k in _CAND_COLS}
_TOR_CAND_COLS = tuple(_CAND_COL_OF[ck] for _, ck in _TOR_DISPLAY)


def _load_torrents(job_ids: list[str]) -> dict[str, list[dict]]:
    """{job_id: torrents} lidos das tabelas (com _lock). Os campos de um
    torrent que aponta para candidato vêm do candidato (JOIN)."""
    out: dict[str, list[dict]] = {}
    tcols = ", ".join(f"t.{c}" for c in _TOR_COLS)
    ccols = ", ".join(f"c.{c}" for c in _TOR_CAND_COLS)
    ncand = len(_TOR_CAND_COLS)
    for chunk, ph in _in_chunks(job_ids):
        kids: dict[tuple, tuple[list, list]] = {}
        for jid, n, key, pos, shrunk in _conn.execute(
                f"SELECT job_id, n, ep_key, pos, shrunk FROM torrent_coverage "
                f"WHERE job_id IN ({ph})", chunk):
            kids.setdefault((jid, n), ([], []))[0].append((key, pos, shrunk))
        for jid, n, pos, path in _conn.execute(
                f"SELECT job_id, n, pos, path FROM torrent_files "
                f"WHERE job_id IN ({ph}) ORDER BY job_id, n, pos", chunk):
            kids.setdefault((jid, n), ([], []))[1].append((pos, path))
        for r in _conn.execute(
                f"SELECT t.job_id, t.n, {tcols}, c.cid IS NOT NULL, {ccols} "
                f"FROM job_torrents t LEFT JOIN search_candidates c "
                f"ON c.job_id = t.job_id AND c.cid = t.cid "
                f"WHERE t.job_id IN ({ph}) ORDER BY t.job_id, t.n", chunk):
            row = r[2:2 + len(_TOR_COLS)]
            has_cand = r[2 + len(_TOR_COLS)]
            cand = tuple(r[-ncand:]) if has_cand else None
            out.setdefault(r[0], []).append(_torrent_from_row(
                r[1], row, kids.get((r[0], r[1]), ([], [])), cand))
    return out


def _legacy_series(doc: dict) -> dict:
    """Documento de série no formato antigo (episódios, order_map e torrents
    dentro de jobs.data) -> o dict como as tabelas o devolvem (função pura).

    Passa cada episódio/torrent pela MESMA conversão das tabelas (ida e
    volta), então o dict é igual ao que sai do banco depois do próximo
    upsert: legenda vazia some, chave desconhecida é preservada, torrents
    em ordem de n, a EDL passa pela conversão das tabelas de EDL. `report`
    sai — é derivado (merge_runner.report_view)."""
    doc = dict(doc)
    doc.pop("report", None)
    if "episodes" in doc:
        om_old = doc.get("order_map") or {}
        eps, om = {}, {}
        for pos, (key, ep) in enumerate((doc.get("episodes") or {}).items()):
            eps[key], alt = _episode_from_row(*_episode_row(pos, ep, om_old.get(key)))
            if ep.get("edl") is not None:
                eps[key]["edl"] = _edl_round_trip(ep["edl"])
            if alt:
                om[key] = alt
        doc["episodes"], doc["order_map"] = eps, om or None
    if "torrents" in doc:
        doc["torrents"] = [
            _torrent_from_row(t["n"], *_torrent_row(t, None), None)
            for t in sorted(doc.get("torrents") or [], key=lambda t: t["n"])]
    return doc


# -------------------- EDLs (alinhador por conteúdo) --------------------
# Formato em memória (o de sempre — services/series/align/edl.py), em
# job["episodes"][k]["edl"] (série) ou job["advanced"]["edl"] (filme):
#   {version, episode, source_dub{path, duration}, source_orig{...},
#    segments[{kind, a_start, a_end, b_start, b_end, slope, residual,
#              confidence, offset, note, action?, refine?, extra?}],
#    confidence_profile[um valor por segundo], review{required, flagged},
#    merged_side?, a_window?/b_window? [início, fim], note?}
# Vira: uma linha em edls (ep_key = chave do episódio, ou "filme") e uma por
# segmento em edl_segments. O dict em memória não muda — quem aplica a
# decisão de revisão (`action`) continua mexendo nele direto.
#
# Round-trip EXATO por construção: cada chave só vai para a coluna dela
# quando o valor tem o tipo da coluna (float em REAL, str em TEXT...). O
# resto — chave desconhecida, int onde se espera float, chave padrão
# ausente, None em chave opcional, estrutura com outra forma — vai para
# `spill` (JSON) e a leitura devolve no mesmo lugar. EDL gerada pelo
# alinhador nunca usa o spill; EDL montada à mão (testes, jobs antigos)
# volta igual mesmo assim.
#
# confidence_profile: ~2.500 valores por episódio (13,6 KB em JSON) — não
# vira linha, vai num BLOB float32. Os valores saem do edl.build
# arredondados a 3 casas e só alimentam o gráfico da revisão (o rebuild da
# EDL arredonda a 3 casas de novo); float32 arredondado a 3 casas na leitura
# devolve EXATAMENTE o mesmo float64 de [0, 1]. A gravação confere a volta:
# se não bater (valor grande demais para float32 ter as 3 casas, ou com mais
# casas), grava float64 — sem perda em nenhum caso. profile_digits = 3 marca
# o float32; NULL, float64 cru.

def _create_edl_tables():
    _conn.execute("""
        CREATE TABLE IF NOT EXISTS edls (
            job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
            ep_key TEXT NOT NULL,
            version INTEGER,
            episode TEXT,
            src_dub_path TEXT,
            src_dub_duration REAL,
            src_orig_path TEXT,
            src_orig_duration REAL,
            review_required INTEGER,
            review_flagged TEXT,
            merged_side TEXT,
            a_win_start REAL,
            a_win_end REAL,
            b_win_start REAL,
            b_win_end REAL,
            note TEXT,
            spill TEXT,
            confidence_profile BLOB,
            profile_digits INTEGER,
            PRIMARY KEY (job_id, ep_key)
        )""")
    _conn.execute("""
        CREATE TABLE IF NOT EXISTS edl_segments (
            job_id TEXT NOT NULL,
            ep_key TEXT NOT NULL,
            pos INTEGER NOT NULL,
            kind TEXT,
            a_start REAL,
            a_end REAL,
            b_start REAL,
            b_end REAL,
            slope REAL,
            residual REAL,
            confidence REAL,
            seg_offset REAL,
            note TEXT,
            action TEXT,
            refine TEXT,
            extra TEXT,
            spill TEXT,
            PRIMARY KEY (job_id, ep_key, pos),
            FOREIGN KEY (job_id, ep_key)
                REFERENCES edls(job_id, ep_key) ON DELETE CASCADE
        )""")


EDL_MOVIE_KEY = "filme"   # ep_key da EDL de filme (é o `episode` dela também)

# o dict carregado SEM a seção das EDLs (listagens) leva esta chave: o
# upsert de um dict assim não mexe nas EDLs gravadas — um episódio sem
# "edl" ali não quer dizer "EDL apagada". Nunca vai para jobs.data nem
# para a API (jobs.runtime._NOT_PUBLIC).
EDLS_UNLOADED = "_edls_unloaded"

# chave -> tipo aceito na coluna: f = float (REAL), s = str (TEXT),
# d = dict (TEXT com JSON). Ordem = a do edl._seg_dict (asdict + opcionais).
_SEG_STD = ("kind", "a_start", "a_end", "b_start", "b_end", "slope",
            "residual", "confidence", "offset", "note")
_SEG_OPT = ("action", "refine", "extra")
_SEG_TYPE = {"kind": "s", "a_start": "f", "a_end": "f", "b_start": "f",
             "b_end": "f", "slope": "f", "residual": "f", "confidence": "f",
             "offset": "f", "note": "s", "action": "s", "refine": "s",
             "extra": "d"}
# colunas de edl_segments, na ordem de _SEG_STD + _SEG_OPT (+ spill)
_SEG_COLS = ("kind", "a_start", "a_end", "b_start", "b_end", "slope",
             "residual", "confidence", "seg_offset", "note", "action",
             "refine", "extra", "spill")

# chaves de primeiro nível (ordem do edl.build + o que o alinhador acrescenta)
_EDL_STD = ("version", "episode", "source_dub", "source_orig", "segments",
            "confidence_profile", "review")
_EDL_OPT = ("merged_side", "a_window", "b_window", "note")
_EDL_COLS = ("version", "episode", "src_dub_path", "src_dub_duration",
             "src_orig_path", "src_orig_duration", "review_required",
             "review_flagged", "merged_side", "a_win_start", "a_win_end",
             "b_win_start", "b_win_end", "note", "spill")
_EDL_SRC = (("source_dub", "src_dub"), ("source_orig", "src_orig"))


def _fits(kind: str, v) -> bool:
    if kind == "f":
        return isinstance(v, float)
    if kind == "s":
        return isinstance(v, str)
    return isinstance(v, dict)


def _spill_json(over: dict, absent: list) -> str | None:
    spill = {}
    if over:
        spill["values"] = over
    if absent:
        spill["absent"] = absent
    return _json(spill or None)


def _spill_parts(raw: str | None) -> tuple[dict, set]:
    spill = json.loads(raw) if raw else {}
    return spill.get("values") or {}, set(spill.get("absent") or ())


def _segment_row(seg) -> tuple:
    """Segmento em memória -> valores de _SEG_COLS. Chave padrão com None
    vai NULL na coluna (presente); opcional só existe com coluna não nula."""
    if not isinstance(seg, dict):
        return (None,) * (len(_SEG_COLS) - 1) + (_spill_json({"segment": seg}, []),)
    over = {k: v for k, v in seg.items() if k not in _SEG_TYPE}
    absent = [k for k in _SEG_STD if k not in seg]
    vals = []
    for k in (*_SEG_STD, *_SEG_OPT):
        v = seg.get(k)
        if k not in seg or (v is None and k in _SEG_STD):
            vals.append(None)
        elif _fits(_SEG_TYPE[k], v):
            vals.append(_json(v) if _SEG_TYPE[k] == "d" else v)
        else:
            over[k] = v
            vals.append(None)
    return (*vals, _spill_json(over, absent))


def _segment_from_row(row: tuple):
    over, absent = _spill_parts(row[-1])
    if "segment" in over and len(over) == 1 and not absent \
            and all(v is None for v in row[:-1]):
        return over["segment"]          # o segmento nem era um dict
    seg = {}
    for k, v in zip((*_SEG_STD, *_SEG_OPT), row[:-1]):
        if k in over:
            seg[k] = over[k]
        elif k in _SEG_STD and k not in absent:
            seg[k] = v
        elif k in _SEG_OPT and v is not None:
            seg[k] = json.loads(v) if _SEG_TYPE[k] == "d" else v
    seg.update((k, v) for k, v in over.items() if k not in seg)
    return seg


def _is_window(v) -> bool:
    return (isinstance(v, list) and len(v) == 2
            and all(isinstance(x, float) for x in v))


def _edl_row(edl: dict) -> tuple[tuple, tuple | None, tuple]:
    """EDL em memória -> (valores de _EDL_COLS, perfil, linhas de
    edl_segments). O perfil sai como tupla de floats (o BLOB só é montado
    na hora de gravar — _profile_blob) ou None (sem perfil, ou no spill)."""
    over = {k: v for k, v in edl.items() if k not in _EDL_STD + _EDL_OPT}
    absent = [k for k in _EDL_STD if k not in edl]
    cols: dict = dict.fromkeys(_EDL_COLS)
    profile, segs = None, ()

    def put(key: str, ok: bool, **vals):
        if key not in edl:
            return
        if ok:
            cols.update(vals)
        else:
            over[key] = edl[key]

    v = edl.get("version")
    put("version", v is None or (isinstance(v, int) and not isinstance(v, bool)),
        version=v)
    v = edl.get("episode")
    put("episode", v is None or isinstance(v, str), episode=v)
    for key, pre in _EDL_SRC:
        v = edl.get(key)
        ok = (isinstance(v, dict) and set(v) == {"path", "duration"}
              and (v["path"] is None or isinstance(v["path"], str))
              and (v["duration"] is None or isinstance(v["duration"], float)))
        put(key, ok, **({f"{pre}_path": v["path"],
                         f"{pre}_duration": v["duration"]} if ok else {}))
    v = edl.get("segments")
    if isinstance(v, list):
        segs = tuple((i, *_segment_row(s)) for i, s in enumerate(v))
    else:
        put("segments", False)
    v = edl.get("confidence_profile")
    if isinstance(v, list) and all(isinstance(x, float) for x in v):
        profile = tuple(v)
    else:
        put("confidence_profile", v is None)
    v = edl.get("review")
    ok = (isinstance(v, dict) and set(v) == {"required", "flagged"}
          and isinstance(v["required"], bool))
    put("review", ok, **({"review_required": int(v["required"]),
                          "review_flagged": _json(v["flagged"])} if ok else {}))
    for key in ("merged_side", "note"):
        put(key, isinstance(edl.get(key), str), **{key: edl.get(key)})
    for key, pre in (("a_window", "a_win"), ("b_window", "b_win")):
        v = edl.get(key)
        put(key, _is_window(v),
            **({f"{pre}_start": v[0], f"{pre}_end": v[1]} if _is_window(v) else {}))
    cols["spill"] = _spill_json(over, absent)
    return tuple(cols[c] for c in _EDL_COLS), profile, segs


def _edl_from_row(row: tuple, blob: bytes | None, digits: int | None,
                  segs) -> dict:
    """Linha de edls (+ perfil, + segmentos) -> EDL em memória."""
    v = dict(zip(_EDL_COLS, row))
    over, absent = _spill_parts(v["spill"])
    edl: dict = {}

    def get(key: str, present: bool, make):
        if key in over:
            edl[key] = over[key]
        elif present:
            edl[key] = make()

    get("version", "version" not in absent, lambda: v["version"])
    get("episode", "episode" not in absent, lambda: v["episode"])
    for key, pre in _EDL_SRC:
        get(key, key not in absent, lambda pre=pre: {
            "path": v[f"{pre}_path"], "duration": v[f"{pre}_duration"]})
    get("segments", "segments" not in absent,
        lambda: [_segment_from_row(s[1:]) for s in segs])
    get("confidence_profile", "confidence_profile" not in absent,
        lambda: _profile_from_blob(blob, digits))
    get("review", "review" not in absent, lambda: {
        "required": bool(v["review_required"]),
        "flagged": (json.loads(v["review_flagged"])
                    if v["review_flagged"] is not None else None)})
    get("merged_side", v["merged_side"] is not None, lambda: v["merged_side"])
    for key, pre in (("a_window", "a_win"), ("b_window", "b_win")):
        get(key, v[f"{pre}_start"] is not None,
            lambda pre=pre: [v[f"{pre}_start"], v[f"{pre}_end"]])
    get("note", v["note"] is not None, lambda: v["note"])
    edl.update((k, x) for k, x in over.items() if k not in edl)
    return edl


def _profile_from_blob(blob: bytes | None, digits: int | None) -> list | None:
    if blob is None:
        return None
    if digits is None:
        return np.frombuffer(blob, dtype="<f8").tolist()
    return np.round(np.frombuffer(blob, dtype="<f4").astype("<f8"), digits).tolist()


def _profile_blob(profile: tuple | None) -> tuple[bytes | None, int | None]:
    """Perfil -> (BLOB, profile_digits): float32 quando a volta é exata (o
    caso de todo perfil gerado pelo alinhador), senão float64."""
    if profile is None:
        return None, None
    with np.errstate(over="ignore", invalid="ignore"):
        f32 = np.asarray(profile, dtype="<f4").tobytes()
    if _profile_from_blob(f32, 3) == list(profile):
        return f32, 3
    return np.asarray(profile, dtype="<f8").tobytes(), None


def _edl_digest(edl: dict) -> str:
    """Hash da EDL para a detecção de mudança — sem montar as linhas nem
    passar pelo JSON: repr do resto (em C) e o perfil (~2.500 floats, a
    parte cara de serializar) em bytes float64 crus. ~80 µs por EDL de
    episódio, contra ~350 µs do json.dumps dela inteira."""
    h = hashlib.sha1(repr([(k, v) for k, v in edl.items()
                           if k != "confidence_profile"]).encode("utf-8"))
    prof = edl.get("confidence_profile")
    h.update(b"\0P" if "confidence_profile" in edl else b"\0-")
    try:
        h.update(array("d", prof).tobytes())
    except TypeError:                 # None, ou perfil fora do padrão
        h.update(repr(prof).encode("utf-8"))
    return h.hexdigest()


def _edl_round_trip(edl: dict) -> dict:
    """A EDL como as tabelas a devolvem (para a normalização legada sair
    igual ao que o banco devolve depois do próximo upsert)."""
    row, profile, segs = _edl_row(edl)
    return _edl_from_row(row, *_profile_blob(profile), segs)


def _edl_items(job: dict) -> dict[str, tuple] | None:
    """{ep_key: (hash, linhas | None)} das EDLs do dict — None quando o dict
    não carrega a seção (aí as linhas gravadas ficam como estão). Montado
    fora do lock.

    Cada EDL tem o seu hash: concluir um episódio, ou mudar o estado dele,
    não regrava EDL nenhuma; decidir a revisão de um regrava só a dele. As
    linhas (_edl_row) só são montadas para as EDLs cujo hash difere do
    gravado — as outras ~250 de uma série inteira só pagam o hash."""
    if job.get(EDLS_UNLOADED):
        return None
    if job.get("media_type") == "tv":
        if "episodes" not in job:
            return None
        edls = {k: ep["edl"] for k, ep in job["episodes"].items()
                if ep.get("edl") is not None}
    else:
        adv = job.get("advanced")
        edl = adv.get("edl") if isinstance(adv, dict) else None
        edls = {EDL_MOVIE_KEY: edl} if edl is not None else {}
    cached = (_section_hash.get(job["id"]) or {}).get("edls") or {}
    out = {}
    for k, edl in edls.items():
        h = _edl_digest(edl)
        out[k] = (h, None if cached.get(k) == h else _edl_row(edl))
    return out


def _write_edls(job_id: str, items: dict, cached: dict | None) -> list[str]:
    """Grava as EDLs que mudaram (todas, sem cache) e apaga as que saíram do
    dict. Com _lock, na transação do upsert. Devolve as EDLs que mudaram mas
    ficaram sem gravar: as linhas foram montadas contra o hash da hora do
    preparo, e outra gravação do mesmo job no meio mudou o cache — o próximo
    upsert as grava."""
    if cached is None:
        gone = [r[0] for r in _conn.execute(
            "SELECT ep_key FROM edls WHERE job_id = ?", (job_id,))
                if r[0] not in items]
        changed = list(items)
    else:
        gone = [k for k in cached if k not in items]
        changed = [k for k, it in items.items() if cached.get(k) != it[0]]
    # a linha da EDL que muda sai (com os segmentos) e volta inteira; a que
    # ficou sem linhas montadas fica como está até o próximo upsert
    missed = [k for k in changed if items[k][1] is None]
    stale = gone + [k for k in changed if items[k][1] is not None]
    if stale:
        _conn.executemany("DELETE FROM edls WHERE job_id = ? AND ep_key = ?",
                          [(job_id, k) for k in stale])
    cols = ", ".join(_EDL_COLS)
    ph = ", ".join("?" for _ in range(len(_EDL_COLS) + 4))
    seg_cols = ", ".join(_SEG_COLS)
    seg_ph = ", ".join("?" for _ in range(len(_SEG_COLS) + 3))
    for k in changed:
        if items[k][1] is None:
            continue
        row, profile, segs = items[k][1]
        _conn.execute(
            f"INSERT INTO edls (job_id, ep_key, {cols}, confidence_profile, "
            f"profile_digits) VALUES ({ph})",
            (job_id, k, *row, *_profile_blob(profile)))
        _conn.executemany(
            f"INSERT INTO edl_segments (job_id, ep_key, pos, {seg_cols}) "
            f"VALUES ({seg_ph})", [(job_id, k, *s) for s in segs])
    if cached is None:
        # a EDL que ainda estava na coluna provisória de job_episodes veio
        # para o dict ao carregar: agora o dict manda (gravada aqui, ou
        # descartada — rematch) e a cópia velha sai, senão a leitura a acha
        old = [r[0] for r in _conn.execute(
            "SELECT ep_key FROM job_episodes WHERE job_id = ? "
            "AND edl IS NOT NULL", (job_id,)) if r[0] not in missed]
        _conn.executemany(
            "UPDATE job_episodes SET edl = NULL WHERE job_id = ? AND ep_key = ?",
            [(job_id, k) for k in old])
    return missed


def _load_edls(job_ids: list[str]) -> dict[str, dict[str, dict]]:
    """{job_id: {ep_key: EDL}} lidos das tabelas (com _lock)."""
    out: dict[str, dict[str, dict]] = {}
    cols = ", ".join(_EDL_COLS)
    seg_cols = ", ".join(_SEG_COLS)
    for chunk, ph in _in_chunks(job_ids):
        segs: dict[tuple, list] = {}
        for r in _conn.execute(
                f"SELECT job_id, ep_key, pos, {seg_cols} FROM edl_segments "
                f"WHERE job_id IN ({ph}) ORDER BY job_id, ep_key, pos", chunk):
            segs.setdefault((r[0], r[1]), []).append(r[2:])
        for r in _conn.execute(
                f"SELECT job_id, ep_key, {cols}, confidence_profile, "
                f"profile_digits FROM edls WHERE job_id IN ({ph})", chunk):
            out.setdefault(r[0], {})[r[1]] = _edl_from_row(
                r[2:-2], r[-2], r[-1], segs.get((r[0], r[1]), ()))
    return out


def _has_legacy_edls(doc: dict) -> bool:
    """EDL de filme ainda dentro de jobs.data, ou cópia de EDL gravada no
    payload do gate de revisão (série ou filme)."""
    gate = doc.get("awaiting")
    if (isinstance(gate, dict) and gate.get("reason") == "alignment_review"
            and gate.get("payload") is not None):
        return True
    adv = doc.get("advanced")
    return (doc.get("media_type") != "tv" and isinstance(adv, dict)
            and adv.get("edl") is not None)


def _legacy_edls(doc: dict) -> dict:
    """Documento com EDL no formato antigo -> normalizado (função pura).

    - o gate de revisão de alinhamento guardava uma CÓPIA das EDLs no
      payload ({"episodes": {ep_key|"filme": edl}}); agora o payload é
      derivado (pipeline.awaiting_view / advanced.review_payload): sai;
    - a EDL do filme (advanced.edl) passa pela conversão das tabelas; a do
      episódio de série, dentro do documento, é tratada em _legacy_series.
    """
    doc = dict(doc)
    gate = doc.get("awaiting")
    if isinstance(gate, dict) and gate.get("reason") == "alignment_review":
        doc["awaiting"] = {**gate, "payload": None}
    adv = doc.get("advanced")
    if (doc.get("media_type") != "tv" and isinstance(adv, dict)
            and adv.get("edl") is not None):
        doc["advanced"] = {**adv, "edl": _edl_round_trip(adv["edl"])}
    return doc


def _without_edls(doc: dict) -> dict:
    """O dict sem a seção das EDLs (listagens): tira as que vieram de dentro
    do documento antigo e marca o dict (EDLS_UNLOADED)."""
    eps = doc.get("episodes")
    if isinstance(eps, dict) and any("edl" in ep for ep in eps.values()):
        doc["episodes"] = {k: {kk: vv for kk, vv in ep.items() if kk != "edl"}
                           for k, ep in eps.items()}
    adv = doc.get("advanced")
    if isinstance(adv, dict) and "edl" in adv:
        doc["advanced"] = {k: v for k, v in adv.items() if k != "edl"}
    doc[EDLS_UNLOADED] = True
    return doc


# -------------------- escalares do job e metadados do TMDB --------------------
# O que sobrou no documento depois das listas: os escalares do job viram
# colunas da `jobs` (as listagens filtram/ordenam por elas sem abrir JSON) e
# os pedaços com forma fixa viram colunas ou tabelas pequenas:
#   movie            -> media (tmdb_id + tipo) + media_localized (+ idioma)
#   known_seasons    -> media_seasons        (por série, compartilhada)
#   episode_groups   -> media_episode_groups (por série, compartilhada)
#   progress         -> job_progress (download do filme; o merge é efêmero:
#                       só a etapa em que parou, colunas merge_*)
#   request          -> job_requests;  manual_rows -> manual_rows
#   recompress.rels  -> recompress_items;  drift_confirm.profile -> drift_profile
#   awaiting         -> awaiting_reason (o payload é sempre derivado)
# O dict em memória não muda: a decomposição e a recomposição são daqui.
#
# Round-trip EXATO por construção, como nas EDLs: cada chave só vai para a
# coluna quando o valor tem a forma esperada; o que não cabe (tipo diferente,
# estrutura com outra forma, chave que este formato não conhece) continua no
# documento e a leitura o devolve como estava. Presença de chave é a coluna
# `keys`: um bit por chave de _MASK_KEYS (chave ausente != chave com None —
# filme não tem `media_type`, job de série nasce com `awaiting: None`).
# `keys` NULL = linha gravada antes deste formato (tudo no documento).
#
# Metadados do TMDB (`movie`, temporadas, ordens alternativas) são da OBRA, não
# do job: dezenas de jobs repetem o mesmo tmdb_id (retry, um idioma por job,
# temporadas em jobs separados). Moram uma vez por (tmdb_id, tipo) — os ids de
# filme e de série do TMDB são espaços diferentes. Título localizado, sinopse
# e pôster dependem do IDIOMA em que o job buscou (o TMDB devolve o pôster do
# idioma pedido), então ficam em media_localized por (tmdb_id, tipo, idioma);
# título original, título em inglês, idioma original e ano não dependem.
# Última gravação vence: é cache do TMDB, e o job que buscou por último tem o
# dado mais novo — um job antigo passa a mostrar o pôster atualizado.
# known_seasons/episode_groups do job são DERIVADOS dessas tabelas (o bit diz
# que o job os tem; o conteúdo é o da série).

# colunas da `jobs` além de id/created_at/status/data (ALTER em bancos antigos)
_JOB_COLUMNS = (
    ("keys", "INTEGER"),
    ("media_type", "TEXT NOT NULL DEFAULT 'movie'"),
    ("tmdb_id", "INTEGER"), ("language", "TEXT"), ("mode", "TEXT"),
    ("kind", "TEXT"), ("detail", "TEXT"), ("error", "TEXT"),
    ("output", "TEXT"), ("merge_started_at", "TEXT"),
    ("download_only", "INTEGER"), ("skip_search", "INTEGER"),
    # destino e alvo dos torrents: SNAPSHOT da hora da criação (o registro
    # histórico de para onde o job entregou não muda se o destino for editado
    # ou apagado depois — por isso não é FK)
    ("destination_id", "INTEGER"), ("destination_label", "TEXT"),
    ("destination_path", "TEXT"), ("torrent_target_id", "INTEGER"),
    ("torrent_target_label", "TEXT"), ("torrent_save_path", "TEXT"),
    ("torrent_local_path", "TEXT"),
    # flags do pipeline de série
    ("manual_done", "INTEGER"), ("phase2_done", "INTEGER"),
    ("gaps_accepted", "INTEGER"), ("signals_accepted", "INTEGER"),
    ("rematch_done", "INTEGER"), ("force_continue", "INTEGER"),
    ("rematch_attempts", "INTEGER"),
    ("awaiting_reason", "TEXT"),
    ("media_ref", "INTEGER"),          # 1 = job["movie"] vem de media*
    ("manual_video", "TEXT"), ("manual_audio", "TEXT"),
    ("src_root_video", "TEXT"), ("src_root_audio", "TEXT"),
    ("adv_video_file", "TEXT"), ("adv_audio_file", "TEXT"),
    ("recompress_folder", "TEXT"), ("recompress_rel", "TEXT"),
    ("recompress_replace", "INTEGER"),
    ("request_ref", "INTEGER"),        # 1 = job["request"] em job_requests
    ("drift_video_file", "TEXT"), ("drift_audio_file", "TEXT"),
    ("drift_tau1_ms", "REAL"), ("drift_tau2_ms", "REAL"),
    ("drift_verdict", "TEXT"), ("drift_profiled", "INTEGER"),
    # em que etapa o merge PAROU (ver _enc_progress): fase + qual arquivo o
    # fingerprint lia — não a barra
    ("merge_phase", "TEXT"), ("merge_step", "INTEGER"), ("merge_label", "TEXT"),
)
_JCOLS = tuple(c for c, _ in _JOB_COLUMNS)

# tabelas filhas pequenas do job: (tabela, colunas além de job_id). Cada uma é
# reescrita inteira quando o hash das linhas dela muda.
_KID_TABLES = {
    "job_progress": ("role", "pct", "speed", "eta", "state", "seeds", "name",
                     "size", "downloaded"),
    "job_requests": ("pos", "season", "episode"),
    "manual_rows": ("pos", "season", "episode", "original", "dubbed", "name"),
    "recompress_items": ("pos", "rel"),
    "drift_profile": ("pos", "t", "offset_ms", "quality"),
}
_KID_DECL = {"role": "TEXT NOT NULL", "pos": "INTEGER NOT NULL",
             "pct": "REAL", "t": "REAL", "offset_ms": "REAL", "quality": "REAL",
             "season": "INTEGER", "episode": "INTEGER", "speed": "INTEGER",
             "eta": "INTEGER", "seeds": "INTEGER", "size": "INTEGER",
             "downloaded": "INTEGER"}

_MOVIE_KEYS = ("id", "original_title", "localized_title", "english_title",
               "original_language", "year", "overview", "poster")
_MEDIA_BASE = ("original_title", "english_title", "original_language", "year")
_MEDIA_LOCAL = ("localized_title", "overview", "poster")
_GROUP_KEYS = ("id", "name", "type", "group_count", "episode_count")

# gates de série cujo payload é derivado na leitura (pipeline.DERIVED_GATES —
# um teste confere que as listas são iguais; o store não importa o pipeline):
# payload gravado por versões antigas é descartado na normalização
_DERIVED_TV_GATES = ("gaps_confirm", "alignment_review", "mismatched_pairs",
                     "manual_pick", "incompatible_torrents")


def _create_scalar_tables():
    cols = {r[1] for r in _conn.execute("PRAGMA table_info(jobs)")}
    for col, decl in _JOB_COLUMNS:
        if col not in cols:
            _conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} {decl}")
    # o índice de mídia era sobre json_extract(data, '$.media_type'): agora
    # é a coluna (o filtro de mídia das listagens e das contagens)
    old = _conn.execute("SELECT sql FROM sqlite_master WHERE type = 'index' "
                        "AND name = 'idx_jobs_media'").fetchone()
    if old and "json_extract" in (old[0] or ""):
        _conn.execute("DROP INDEX idx_jobs_media")
    _conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_media "
                  "ON jobs(media_type, status)")
    # linhas do formato antigo (tudo no documento) também precisam da coluna
    # de mídia certa para os filtros; o resto da linha fica para a migração
    _conn.execute(
        "UPDATE jobs SET media_type = 'tv' WHERE keys IS NULL "
        "AND media_type != 'tv' AND CASE WHEN json_valid(data) "
        "THEN json_extract(data, '$.media_type') END = 'tv'")
    for table, kcols in _KID_TABLES.items():
        decl = ", ".join(f"{c} {_KID_DECL.get(c, 'TEXT')}" for c in kcols)
        _conn.execute(f"""
            CREATE TABLE IF NOT EXISTS {table} (
                job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                {decl},
                PRIMARY KEY (job_id, {kcols[0]})
            )""")
    _conn.execute("""
        CREATE TABLE IF NOT EXISTS media (
            tmdb_id INTEGER NOT NULL,
            media_type TEXT NOT NULL,
            original_title TEXT,
            english_title TEXT,
            original_language TEXT,
            year TEXT,
            PRIMARY KEY (tmdb_id, media_type)
        )""")
    _conn.execute("""
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
    _conn.execute("""
        CREATE TABLE IF NOT EXISTS media_seasons (
            tmdb_id INTEGER NOT NULL,
            season INTEGER NOT NULL,
            PRIMARY KEY (tmdb_id, season)
        )""")
    _conn.execute("""
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


def _fit(kind: str, v) -> bool:
    """O valor cabe na coluna sem mudar na volta? (None cabe sempre)"""
    if v is None:
        return True
    if kind == "s":
        return isinstance(v, str)
    if kind == "b":
        return isinstance(v, bool)
    if kind == "i":
        return isinstance(v, int) and not isinstance(v, bool)
    # REAL: int também volta igual (== em Python) — só para números de tela
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _media_type_of(job: dict) -> str:
    """O que a coluna media_type guarda (jobs de filme não têm a chave)."""
    v = job.get("media_type")
    return v if isinstance(v, str) else "movie"


# ---- codecs: valor do job -> (colunas, linhas filhas, linhas compartilhadas)
# ou None (não cabe: fica no documento); e colunas/linhas -> valor ----

def _plain(key: str, col: str, kind: str):
    def enc(v, job):
        return ({col: v}, {}, {}) if _fit(kind, v) else None

    def dec(c, kids):
        v = c[col]
        return bool(v) if kind == "b" and v is not None else v
    return enc, dec


def _enc_media_type(v, job):
    return ({"media_type": v}, {}, {}) if isinstance(v, str) else None


def _enc_movie(v, job):
    if v is None:
        return {"media_ref": None}, {}, {}
    tmdb, lang = job.get("tmdb_id"), job.get("language")
    if not (isinstance(v, dict) and set(v) == set(_MOVIE_KEYS)
            and _fit("i", tmdb) and tmdb is not None and v["id"] == tmdb
            and isinstance(v["id"], int) and isinstance(lang, str)
            and all(_fit("s", v[k]) for k in _MOVIE_KEYS[1:])):
        return None
    mt = _media_type_of(job)
    return ({"media_ref": 1}, {},
            {"media": ((tmdb, mt, *(v[k] for k in _MEDIA_BASE)),
                       (tmdb, mt, lang, *(v[k] for k in _MEDIA_LOCAL)))})


def _dec_movie(c, kids):
    if not c["media_ref"]:
        return None
    m = {"id": c["tmdb_id"]}
    for k in _MOVIE_KEYS[1:]:
        m[k] = c[f"m_{k}"]
    return m


def _enc_awaiting(v, job):
    if v is None:
        return {"awaiting_reason": None}, {}, {}
    if (isinstance(v, dict) and set(v) == {"reason", "payload"}
            and isinstance(v["reason"], str) and v["payload"] is None):
        return {"awaiting_reason": v["reason"]}, {}, {}
    return None


def _dec_awaiting(c, kids):
    r = c["awaiting_reason"]
    return None if r is None else {"reason": r, "payload": None}


_PROG_TYPES = dict(zip(_PROGRESS_KEYS, "fiisisii"))


# o que sobra da barra do merge no banco: a ETAPA, não o andamento
_MERGE_STAGE = (("phase", "merge_phase", "s"), ("step", "merge_step", "i"),
                ("label", "merge_label", "s"))
# só job que parou no meio do merge mostra a etapa relida do banco: ativo
# recomeça o merge do zero depois de um restart (a etapa gravada é velha) e
# job concluído já zerou a barra
_MERGE_STAGE_STATUSES = ("error", "cancelled")


def _merge_stage_cols(m) -> dict:
    """progress.merge -> colunas merge_* (o que não tiver a forma vira None:
    a barra é efêmera, não há o que preservar no documento)."""
    m = m if isinstance(m, dict) else {}
    return {col: (m.get(k) if m.get(k) is not None and _fit(t, m.get(k))
                  else None)
            for k, col, t in _MERGE_STAGE}


def _enc_progress(v, job):
    """Download do filme (video/audio). A barra do `merge` é efêmera: só
    existe enquanto o merge roda neste processo — depois de um restart o
    merge recomeça do zero. Fica só a ETAPA em que ele estava (colunas
    merge_*): um job que falhou no fingerprint mostrava "Buscando
    alinhamento (Áudio)" no detalhe e, sem ela, passou a mostrar "Conversão".
    As colunas vão na linha da `jobs`, que toda gravação já reescreve — e o
    on_progress do ffmpeg não grava nada (só a próxima gravação do job leva
    a etapa que estiver em memória): nenhuma escrita a mais por %."""
    if not isinstance(v, dict):
        return None
    roles = () if job.get("media_type") == "tv" else ("video", "audio")
    if set(v) - {"merge"} != set(roles):
        return None
    rows = []
    for role in roles:
        p = v[role]
        if p is None:
            continue
        if not (isinstance(p, dict) and set(p) == set(_PROGRESS_KEYS)
                and all(_fit(_PROG_TYPES[k], p[k]) for k in _PROGRESS_KEYS)):
            return None
        rows.append((role, *(p[k] for k in _PROGRESS_KEYS)))
    return _merge_stage_cols(v.get("merge")), {"job_progress": tuple(rows)}, {}


# as duas linhas de job_progress vêm no próprio SELECT do job (LEFT JOIN
# por papel, colunas pv_*/pa_*): a listagem não paga uma consulta a mais
_PROG_PREFIX = (("video", "pv"), ("audio", "pa"))
_PROG_NAMES = tuple(f"{pre}_{k}" for _, pre in _PROG_PREFIX
                    for k in ("role",) + _PROGRESS_KEYS)


def _progress_cols(rows) -> dict:
    """Linhas de job_progress -> as colunas pv_*/pa_* do SELECT do job."""
    by_role = {r[0]: r for r in rows}
    out = {}
    for role, pre in _PROG_PREFIX:
        r = by_role.get(role) or (None,) * (len(_PROGRESS_KEYS) + 1)
        out.update(zip((f"{pre}_{k}" for k in ("role",) + _PROGRESS_KEYS), r))
    return out


def _dec_progress(c, kids):
    out = {} if c["media_type"] == "tv" else {
        role: ({k: c[f"{pre}_{k}"] for k in _PROGRESS_KEYS}
               if c[f"{pre}_role"] is not None else None)
        for role, pre in _PROG_PREFIX}
    if c.get("status") in _MERGE_STAGE_STATUSES:
        stage = {k: c[col] for k, col, _ in _MERGE_STAGE if c[col] is not None}
        if stage:
            out["merge"] = stage
    return out


def _enc_pair(cols: tuple[str, str], keys: tuple[str, str], partial: bool,
              ignore: tuple[str, ...] = ()):
    """Dict de dois caminhos (manual_files, src_roots, advanced): cada chave
    numa coluna. `partial`: as chaves podem faltar (src_roots de um papel só)
    e None não cabe; senão as duas são obrigatórias e None = colunas NULL.
    `ignore`: chaves que moram em outra seção (a EDL do filme em `advanced`)."""
    def enc(v, job):
        if v is None:
            return None if partial else (dict.fromkeys(cols), {}, {})
        if not isinstance(v, dict):
            return None
        rest = set(v) - set(ignore)
        if not (rest <= set(keys) if partial else rest == set(keys)):
            return None
        if not all(isinstance(v[k], str) for k in rest):
            return None
        return {c: v.get(k) for c, k in zip(cols, keys)}, {}, {}

    def dec(c, kids):
        if not partial and c[cols[0]] is None:
            return None
        return {k: c[col] for col, k in zip(cols, keys) if c[col] is not None}
    return enc, dec


def _enc_recompress(v, job):
    if not (isinstance(v, dict) and isinstance(v.get("folder"), str)
            and isinstance(v.get("replace"), bool)):
        return None
    if set(v) == {"folder", "rel", "replace"} and isinstance(v["rel"], str):
        rels = ()
    elif (set(v) == {"folder", "rels", "replace"} and isinstance(v["rels"], list)
          and all(isinstance(r, str) for r in v["rels"])):
        rels = tuple(enumerate(v["rels"]))
    else:
        return None
    return ({"recompress_folder": v["folder"], "recompress_rel": v.get("rel"),
             "recompress_replace": v["replace"]},
            {"recompress_items": rels}, {})


def _dec_recompress(c, kids):
    if c["recompress_folder"] is None:
        return None
    out = {"folder": c["recompress_folder"]}
    if c["recompress_rel"] is not None:      # filme: um arquivo
        out["rel"] = c["recompress_rel"]
    else:                                    # série: o lote
        out["rels"] = [r for _, r in kids.get("recompress_items", ())]
    out["replace"] = bool(c["recompress_replace"])
    return out


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _enc_request(v, job):
    """Temporadas inteiras (episode NULL) e episódios avulsos pedidos. Não
    é derivável de job_episodes: o retry recria o job pedindo a TEMPORADA (os
    episódios lançados depois entram), job que falhou antes do TMDB não tem
    episódio nenhum, e a ordem alternativa troca as chaves."""
    if v is None:
        return {"request_ref": None}, {}, {}
    if not (isinstance(v, dict) and set(v) == {"seasons", "episodes"}
            and isinstance(v["seasons"], list)
            and all(_is_int(s) for s in v["seasons"])
            and isinstance(v["episodes"], dict)
            and all(_is_int(s) and isinstance(es, list) and es
                    and all(_is_int(e) for e in es)
                    for s, es in v["episodes"].items())):
        return None
    rows = [(s, None) for s in v["seasons"]]
    rows += [(s, e) for s, es in v["episodes"].items() for e in es]
    return ({"request_ref": 1},
            {"job_requests": tuple((i, s, e) for i, (s, e) in enumerate(rows))}, {})


def _dec_request(c, kids):
    if not c["request_ref"]:
        return None
    out = {"seasons": [], "episodes": {}}
    for _, s, e in kids.get("job_requests", ()):
        if e is None:
            out["seasons"].append(s)
        else:
            out["episodes"].setdefault(s, []).append(e)
    return out


_MANUAL_ROW = (("season", "i"), ("episode", "i"), ("original", "s"),
               ("dubbed", "s"), ("name", "s"))


def _enc_manual_rows(v, job):
    keys = {k for k, _ in _MANUAL_ROW}
    if not (isinstance(v, list) and all(
            isinstance(r, dict) and set(r) == keys
            and all(_fit(t, r[k]) for k, t in _MANUAL_ROW)
            and all(r[k] is not None for k in ("season", "episode",
                                                "original", "dubbed"))
            for r in v)):
        return None
    return {}, {"manual_rows": tuple(
        (i, *(r[k] for k, _ in _MANUAL_ROW)) for i, r in enumerate(v))}, {}


def _dec_manual_rows(c, kids):
    return [dict(zip((k for k, _ in _MANUAL_ROW), r[1:]))
            for r in kids.get("manual_rows", ())]


def _enc_known_seasons(v, job):
    tmdb = job.get("tmdb_id")
    if not (job.get("media_type") == "tv" and _is_int(tmdb)
            and isinstance(v, list) and all(_is_int(s) for s in v)
            and v == sorted(set(v))):
        return None
    return {}, {}, {"seasons": (tmdb, tuple(v))}


def _dec_known_seasons(c, kids):
    return [s for (s,) in kids.get("media_seasons", ())]


def _enc_episode_groups(v, job):
    tmdb = job.get("tmdb_id")
    types = dict(zip(_GROUP_KEYS, "ssiii"))
    if not (job.get("media_type") == "tv" and _is_int(tmdb)
            and isinstance(v, list)
            and all(isinstance(g, dict) and set(g) == set(_GROUP_KEYS)
                    and all(_fit(types[k], g[k]) for k in _GROUP_KEYS)
                    for g in v)):
        return None
    return {}, {}, {"groups": (tmdb, tuple(
        (i, *(g[k] for k in _GROUP_KEYS)) for i, g in enumerate(v)))}


def _dec_episode_groups(c, kids):
    return [dict(zip(_GROUP_KEYS, r[1:])) for r in kids.get("media_episode_groups", ())]


_DRIFT_COLS = (("video_file", "drift_video_file"),
               ("audio_file", "drift_audio_file"),
               ("tau1_ms", "drift_tau1_ms"), ("tau2_ms", "drift_tau2_ms"))
_PROFILE_KEYS = ("t", "offset_ms", "quality")


def _enc_drift(v, job):
    """Pausa de drift do filme: os dois arquivos, os offsets das janelas e o
    perfil medido (uma linha por janela de 5 min)."""
    if v is None:
        return ({c: None for _, c in _DRIFT_COLS}
                | {"drift_verdict": None, "drift_profiled": None}), {}, {}
    base = {k for k, _ in _DRIFT_COLS}
    if not (isinstance(v, dict) and base <= set(v)
            and set(v) <= base | {"profile", "verdict"}
            and isinstance(v["video_file"], str)
            and isinstance(v["audio_file"], str)
            and all(_fit("f", v[k]) and v[k] is not None
                    for k in ("tau1_ms", "tau2_ms"))
            and isinstance(v.get("verdict", ""), str)):
        return None
    prof = v.get("profile", [])
    if not (isinstance(prof, list) and all(
            isinstance(p, dict) and set(p) == set(_PROFILE_KEYS)
            and all(_fit("f", p[k]) and p[k] is not None for k in _PROFILE_KEYS)
            for p in prof)):
        return None
    cols = {c: v[k] for k, c in _DRIFT_COLS}
    cols["drift_verdict"] = v.get("verdict")
    cols["drift_profiled"] = 1 if "profile" in v else 0
    return cols, {"drift_profile": tuple(
        (i, *(p[k] for k in _PROFILE_KEYS)) for i, p in enumerate(prof))}, {}


def _dec_drift(c, kids):
    if c["drift_video_file"] is None:
        return None
    out = {k: c[col] for k, col in _DRIFT_COLS}
    if c["drift_profiled"]:
        out["profile"] = [dict(zip(_PROFILE_KEYS, r[1:]))
                          for r in kids.get("drift_profile", ())]
    if c["drift_verdict"] is not None:
        out["verdict"] = c["drift_verdict"]
    return out


# chaves que são uma coluna só: (chave, coluna, tipo)
_PLAIN_SPECS = (
    ("tmdb_id", "tmdb_id", "i"), ("language", "language", "s"),
    ("mode", "mode", "s"), ("kind", "kind", "s"),
    ("detail", "detail", "s"), ("error", "error", "s"),
    ("output", "output", "s"), ("merge_started_at", "merge_started_at", "s"),
    ("download_only", "download_only", "b"),
    ("skip_search", "skip_search", "b"),
    ("destination_id", "destination_id", "i"),
    ("destination_label", "destination_label", "s"),
    ("destination_path", "destination_path", "s"),
    ("torrent_target_id", "torrent_target_id", "i"),
    ("torrent_target_label", "torrent_target_label", "s"),
    ("torrent_save_path", "torrent_save_path", "s"),
    ("torrent_local_path", "torrent_local_path", "s"),
    ("_manual_done", "manual_done", "b"),
    ("_phase2_done", "phase2_done", "b"),
    ("_gaps_accepted", "gaps_accepted", "b"),
    ("_signals_accepted", "signals_accepted", "b"),
    ("_rematch_done", "rematch_done", "b"),
    ("force_continue", "force_continue", "b"),
    ("rematch_attempts", "rematch_attempts", "i"))

# chave do job -> (codec de ida, de volta). A ORDEM define o bit da chave em
# `keys`: só se acrescenta no fim, nunca se reordena.
_CODECS: dict = {
    "media_type": (_enc_media_type, lambda c, kids: c["media_type"]),
    **{k: _plain(k, col, t) for k, col, t in _PLAIN_SPECS},
    "awaiting": (_enc_awaiting, _dec_awaiting),
    "movie": (_enc_movie, _dec_movie),
    "progress": (_enc_progress, _dec_progress),
    "manual_files": _enc_pair(("manual_video", "manual_audio"),
                              ("video", "audio"), partial=False),
    "src_roots": _enc_pair(("src_root_video", "src_root_audio"),
                           ("video", "audio"), partial=True),
    "advanced": _enc_pair(("adv_video_file", "adv_audio_file"),
                          ("video_file", "audio_file"), partial=False,
                          ignore=("edl",)),
    "recompress": (_enc_recompress, _dec_recompress),
    "request": (_enc_request, _dec_request),
    "manual_rows": (_enc_manual_rows, _dec_manual_rows),
    "known_seasons": (_enc_known_seasons, _dec_known_seasons),
    "episode_groups": (_enc_episode_groups, _dec_episode_groups),
    "drift_confirm": (_enc_drift, _dec_drift),
}
_MASK_KEYS = tuple(_CODECS)
_MASK_BIT = {k: 1 << i for i, k in enumerate(_MASK_KEYS)}
# chaves que moram fora do documento em toda linha do formato novo
_ROW_KEYS = ("id", "created_at", "status")
# tabela filha -> chave do job cujo bit diz se vale a pena consultá-la
# (job_progress não está aqui: vem no próprio SELECT do job, _JOB_SELECT)
_KID_OWNER = {"job_requests": "request",
              "manual_rows": "manual_rows", "recompress_items": "recompress",
              "drift_profile": "drift_confirm"}


def _encode(job: dict) -> dict:
    """Decompõe os escalares do job (função pura, fora do lock):
    {cols: colunas da jobs, kids: linhas das tabelas filhas, shared: linhas
    das tabelas compartilhadas, fitted: chaves que saem do documento}."""
    cols = dict.fromkeys(_JCOLS)
    cols["media_type"] = "movie"
    kids = {t: () for t in _KID_TABLES}
    shared: dict = {}
    mask, fitted = 0, []
    for key, (enc, _) in _CODECS.items():
        if key not in job:
            continue
        out = enc(job[key], job)
        if out is None:
            continue                     # não cabe: fica no documento
        c, k, s = out
        cols.update(c)
        kids.update(k)
        shared.update(s)
        mask |= _MASK_BIT[key]
        fitted.append(key)
    cols["keys"] = mask
    for col in ("download_only", "skip_search", "manual_done", "phase2_done",
                "gaps_accepted", "signals_accepted", "rematch_done",
                "force_continue", "recompress_replace"):
        if cols[col] is not None:
            cols[col] = int(cols[col])
    return {"cols": cols, "kids": kids, "shared": shared, "fitted": fitted}


def _shared_view(shared: dict) -> tuple[dict, dict]:
    """Linhas compartilhadas -> (colunas m_* do JOIN, linhas de temporadas e
    grupos) — o que a leitura acharia nas tabelas depois desta gravação."""
    cols = {f"m_{k}": None for k in _MOVIE_KEYS[1:]}
    if "media" in shared:
        base, loc = shared["media"]
        cols.update(zip((f"m_{k}" for k in _MEDIA_BASE), base[2:]))
        cols.update(zip((f"m_{k}" for k in _MEDIA_LOCAL), loc[3:]))
    kids = {"media_seasons": [(s,) for s in shared.get("seasons", (0, ()))[1]],
            "media_episode_groups": list(shared.get("groups", (0, ()))[1])}
    return cols, kids


# chave simples -> (coluna, é bool?): decodificadas sem chamar função, porque
# a listagem decodifica centenas de linhas por consulta
_PLAIN_COLS = {k: (col, t == "b") for k, col, t in _PLAIN_SPECS}
_PLAIN_COLS["media_type"] = ("media_type", False)
_plans: dict[int, tuple] = {}


def _plan(mask: int) -> tuple:
    """(chaves simples, itemgetter das colunas delas, chaves bool, composta)
    das chaves marcadas em `mask` — calculado uma vez por combinação (são
    poucas: filme, série, recompressão, merge manual...)."""
    p = _plans.get(mask)
    if p is None:
        keys = [k for k in _MASK_KEYS if mask & _MASK_BIT[k]]
        plain = [k for k in keys if k in _PLAIN_COLS and not _PLAIN_COLS[k][1]]
        cols = [_PLAIN_COLS[k][0] for k in plain]
        if len(cols) > 1:
            get = itemgetter(*cols)
        else:   # itemgetter de uma coluna só devolve o valor, não tupla
            get = lambda c, cs=tuple(cols): tuple(c[x] for x in cs)  # noqa: E731
        bools = tuple((k, _PLAIN_COLS[k][0]) for k in keys
                      if k in _PLAIN_COLS and _PLAIN_COLS[k][1])
        comp = tuple((k, _CODECS[k][1]) for k in keys if k not in _PLAIN_COLS)
        p = _plans[mask] = (tuple(plain), get, bools, comp)
    return p


def _decode(c: dict, kids: dict) -> dict:
    """Colunas da linha (+ m_* do JOIN) + linhas filhas -> as chaves do job
    marcadas em `keys`."""
    plain, get, bools, comp = _plan(c["keys"])
    out = dict(zip(plain, get(c)))
    for k, col in bools:
        v = c[col]
        out[k] = None if v is None else bool(v)
    for k, dec in comp:
        out[k] = dec(c, kids)
    return out


def _scalar_digests(enc: dict) -> dict:
    """Hash de cada tabela filha e de cada linha compartilhada do job."""
    out = {t: _digest(rows) for t, rows in enc["kids"].items()}
    for k in ("media", "seasons", "groups"):
        if k in enc["shared"]:
            out[k] = _digest(enc["shared"][k])
    return out


def _legacy_scalars(doc: dict) -> dict:
    """Documento do formato antigo (escalares, movie, request, progress...
    dentro de jobs.data) -> o dict como as colunas/tabelas o devolvem (função
    pura): cada chave que cabe passa pela mesma ida e volta da gravação.

    - request: as temporadas dos episódios avulsos voltam a int (o JSON as
      tinha virado texto: {"3": [1, 5]});
    - progress: a barra do merge sai (efêmera) — num job que parou no meio
      do merge fica só a etapa (ver _enc_progress);
    - awaiting de série com gate derivado: o payload gravado sai;
    - movie e progress de versões antigas completam o formato atual: chaves
      que vieram depois (english_title, original_language, overview; size e
      downloaded do download) entram como None, e `movie.seasons` (lista que
      nenhum código lê mais) sai. Sem isso, o codificador — que exige o
      formato EXATO para a ida e volta não confundir ausente com None —
      deixava esses jobs inteiros no documento (medido no banco real: 62
      jobs com movie e 49 com progress de fora das tabelas). Para esses
      campos ausente e None são a mesma coisa para quem lê."""
    doc = dict(doc)
    mv = doc.get("movie")
    if isinstance(mv, dict):
        doc["movie"] = {k: mv.get(k) for k in _MOVIE_KEYS}
    prog = doc.get("progress")
    if isinstance(prog, dict) and doc.get("media_type") != "tv":
        doc["progress"] = {
            role: ({k: v.get(k) for k in _PROGRESS_KEYS}
                   if role != "merge" and isinstance(v, dict)
                   and set(v) <= set(_PROGRESS_KEYS) else v)
            for role, v in prog.items()}
    req = doc.get("request")
    if isinstance(req, dict) and isinstance(req.get("episodes"), dict):
        eps = req["episodes"]
        if all(isinstance(k, str) and k.isdigit() for k in eps):
            doc["request"] = {**req, "episodes": {int(k): v for k, v in eps.items()}}
    gate = doc.get("awaiting")
    if (doc.get("media_type") == "tv" and isinstance(gate, dict)
            and gate.get("reason") in _DERIVED_TV_GATES
            and gate.get("payload") is not None):
        doc["awaiting"] = {**gate, "payload": None}
    enc = _encode(doc)
    mcols, mkids = _shared_view(enc["shared"])
    c = {**enc["cols"], **mcols, **_progress_cols(enc["kids"]["job_progress"]),
         "status": doc.get("status")}
    kids = {**{t: list(r) for t, r in enc["kids"].items()}, **mkids}
    for key in enc["fitted"]:
        val = _CODECS[key][1](c, kids)
        if key == "advanced" and "edl" in doc["advanced"]:
            val["edl"] = doc["advanced"]["edl"]   # seção própria (edls)
        doc[key] = val
    return doc


def _write_scalar_rows(job_id: str, enc: dict, digests: dict,
                       cached: dict | None):
    """Grava as tabelas filhas e compartilhadas que mudaram (com _lock, na
    transação do upsert)."""
    cached = cached or {}
    for table, kcols in _KID_TABLES.items():
        if cached.get(table) == digests[table]:
            continue
        _conn.execute(f"DELETE FROM {table} WHERE job_id = ?", (job_id,))
        rows = enc["kids"][table]
        if rows:
            ph = ", ".join("?" for _ in range(len(kcols) + 1))
            _conn.executemany(
                f"INSERT INTO {table} (job_id, {', '.join(kcols)}) VALUES ({ph})",
                [(job_id, *r) for r in rows])
    shared = enc["shared"]
    if "media" in shared and cached.get("media") != digests["media"]:
        base, loc = shared["media"]
        # upsert e não INSERT OR REPLACE: o REPLACE apagaria a linha e, pelo
        # CASCADE, os títulos localizados dos OUTROS idiomas
        upd = ", ".join(f"{k} = excluded.{k}" for k in _MEDIA_BASE)
        _conn.execute(
            f"INSERT INTO media (tmdb_id, media_type, {', '.join(_MEDIA_BASE)}) "
            f"VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(tmdb_id, media_type) "
            f"DO UPDATE SET {upd}", base)
        upd = ", ".join(f"{k} = excluded.{k}" for k in _MEDIA_LOCAL)
        _conn.execute(
            f"INSERT INTO media_localized (tmdb_id, media_type, language, "
            f"{', '.join(_MEDIA_LOCAL)}) VALUES (?, ?, ?, ?, ?, ?) "
            f"ON CONFLICT(tmdb_id, media_type, language) DO UPDATE SET {upd}", loc)
    if "seasons" in shared and cached.get("seasons") != digests["seasons"]:
        tmdb, seasons = shared["seasons"]
        _conn.execute("DELETE FROM media_seasons WHERE tmdb_id = ?", (tmdb,))
        _conn.executemany(
            "INSERT INTO media_seasons (tmdb_id, season) VALUES (?, ?)",
            [(tmdb, s) for s in seasons])
    if "groups" in shared and cached.get("groups") != digests["groups"]:
        tmdb, groups = shared["groups"]
        _conn.execute("DELETE FROM media_episode_groups WHERE tmdb_id = ?", (tmdb,))
        _conn.executemany(
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
            for r in _conn.execute(
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
            for r in _conn.execute(
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


# -------------------- composição / decomposição do job --------------------

def _compose(doc: dict, loaded: dict, legacy_scalars: bool = False) -> dict:
    """Documento (colunas + jobs.data, _row_doc) + linhas das tabelas -> o
    dict do job.

    `loaded` traz só as seções pedidas ({"candidates"|"episodes"|"torrents"|
    "edls": linhas do job ou None}); seção não pedida fica fora do dict (sem
    as EDLs, o dict leva a marca EDLS_UNLOADED). Documento no formato antigo
    (seção ainda dentro de jobs.data, ou a EDL na coluna provisória de
    job_episodes, ou os escalares dentro de jobs.data — `legacy_scalars`) é
    normalizado: gravado antes das tabelas, as linhas ainda não existem, e o
    próximo upsert (job ativo) grava no formato novo."""
    jid = doc["id"]
    cache = _section_hash.setdefault(jid, {})
    with_edls = "edls" in loaded
    if _is_legacy(doc):
        doc = _legacy_candidates(doc)
        cache.pop("candidates", None)
    elif "candidates" in loaded:
        doc.update(loaded["candidates"]
                   or {"search_queries": [], "search_candidates": []})
        if with_edls:
            # o que acabou de ser lido É o que está gravado: o próximo upsert
            # sem mudança nos candidatos não reescreve as linhas. Só no dict
            # completo — o de listagem nunca é regravado (e é a listagem que
            # paga isto centenas de vezes por consulta)
            cache["candidates"] = _rows_digest(_section_rows(doc))
    if _has_legacy_edls(doc):
        doc = _legacy_edls(doc)
        cache.pop("edls", None)
    edls = (loaded.get("edls") or {}) if with_edls else {}
    if doc.get("media_type") == "tv":
        legacy = [k for k in ("episodes", "torrents") if k in doc]
        if legacy or "report" in doc or "order_map" in doc:
            doc = _legacy_series(doc)
            for k in legacy:
                cache.pop(k, None)   # tabela desatualizada: reescreve inteira
            if "episodes" in legacy:
                cache.pop("edls", None)
        if "episodes" not in legacy and "episodes" in loaded:
            eps, om, old = loaded["episodes"] or ({}, None, {})
            doc["episodes"], doc["order_map"] = eps, om
            for k, ep in eps.items():
                if k in edls:
                    ep["edl"] = edls[k]
                elif k in old:
                    # EDL ainda na coluna provisória (passo anterior às
                    # tabelas de EDL): o próximo upsert a muda para cá
                    ep["edl"] = _edl_round_trip(old[k])
                    cache.pop("edls", None)
        if "torrents" not in legacy and "torrents" in loaded:
            doc["torrents"] = loaded["torrents"] or []
    else:
        adv = doc.get("advanced")
        if isinstance(adv, dict) and "edl" not in adv and EDL_MOVIE_KEY in edls:
            doc["advanced"] = {**adv, "edl": edls[EDL_MOVIE_KEY]}
    if not with_edls:
        doc = _without_edls(doc)
    if legacy_scalars:
        doc = _legacy_scalars(doc)
        cache.pop("scalars", None)
    elif with_edls:
        # o que acabou de ser lido É o que está gravado (as tabelas filhas e
        # os metadados): o próximo upsert sem mudança não as reescreve — nem
        # devolve à media um título que outro job atualizou depois. Só no
        # dict completo: o de listagem (sem as EDLs) nunca é regravado
        cache["scalars"] = _scalar_digests(_encode(doc))
    if not cache:
        _section_hash.pop(jid, None)
    return doc


def _prepare_upsert(job: dict) -> dict:
    """Documento + linhas de cada seção do job (com os hashes) — tudo
    serializado ANTES de tocar no banco: dict mexido por outra thread no
    meio (RuntimeError) não pode deixar gravação pela metade.

    Seção ausente do dict (carregado sem ela, ou montado à mão) não mexe nas
    linhas gravadas — só quem tem a lista pode reescrevê-la."""
    tv = job.get("media_type") == "tv"
    view = job
    gate = job.get("awaiting")
    if (isinstance(gate, dict) and gate.get("reason") == "alignment_review"
            and gate.get("payload") is not None):
        # cópia das EDLs (dict vindo de fora do fluxo novo, ex. jobs.json
        # antigo): o payload desse gate é sempre derivado, nunca gravado
        view = {**job, "awaiting": {**gate, "payload": None}}
    enc = _encode(view)
    skip = {"events", EDLS_UNLOADED, *_SECTIONS, *_LEGACY_KEYS,
            *(_SERIES_KEYS if tv else ()), *_ROW_KEYS, *enc["fitted"]}
    doc = {k: v for k, v in view.items() if k not in skip}
    adv = doc.get("advanced")
    if not tv and isinstance(adv, dict) and "edl" in adv:
        # filme com `advanced` fora do formato (ficou no documento): a EDL
        # vai para as tabelas mesmo assim
        doc["advanced"] = {k: v for k, v in adv.items() if k != "edl"}
    prog = doc.get("progress")
    if isinstance(prog, dict) and "merge" in prog:
        # progresso fora do formato: a barra do merge é efêmera mesmo assim
        doc["progress"] = {k: v for k, v in prog.items() if k != "merge"}
    prepared: dict = {"data": json.dumps(doc, ensure_ascii=False),
                      "enc": enc, "digests": _scalar_digests(enc),
                      "candidates": None, "episodes": None, "torrents": None,
                      "edls": _edl_items(job)}
    if all(k in job for k in _SECTIONS):
        rows = _section_rows(job)
        prepared["candidates"] = (rows, _rows_digest(rows))
    if tv and "episodes" in job:
        prepared["episodes"] = _episode_items(job)
    if tv and "torrents" in job:
        prepared["torrents"] = _torrent_items(job)
    return prepared


def _apply_upsert(job: dict, prepared: dict, create: bool) -> bool:
    """Escreve o job preparado (com _lock, dentro de uma transação aberta;
    quem chama faz o commit). False = linha inexistente com create=False."""
    data = prepared["data"]
    cols = prepared["enc"]["cols"]
    vals = tuple(cols[c] for c in _JCOLS)
    if create:
        names = ", ".join(_JCOLS)
        ph = ", ".join("?" for _ in range(len(_JCOLS) + 4))
        upd = ", ".join(f"{c} = excluded.{c}" for c in ("status", "data") + _JCOLS)
        _conn.execute(
            f"INSERT INTO jobs (id, created_at, status, data, {names}) "
            f"VALUES ({ph}) ON CONFLICT(id) DO UPDATE SET {upd}",
            (job["id"], job["created_at"], job["status"], data, *vals))
    else:
        upd = ", ".join(f"{c} = ?" for c in ("status", "data") + _JCOLS)
        cur = _conn.execute(f"UPDATE jobs SET {upd} WHERE id = ?",
                            (job["status"], data, *vals, job["id"]))
        if cur.rowcount == 0:
            return False
    cache = _section_hash.get(job["id"]) or {}
    _write_scalar_rows(job["id"], prepared["enc"], prepared["digests"],
                       cache.get("scalars"))
    if prepared["candidates"] is not None:
        rows, digest = prepared["candidates"]
        if cache.get("candidates") != digest:
            _write_sections(job["id"], rows)
    if prepared["episodes"] is not None:
        _write_episodes(job["id"], prepared["episodes"], cache.get("episodes"))
    if prepared["torrents"] is not None:
        _write_torrents(job["id"], prepared["torrents"], cache.get("torrents"))
    if prepared["edls"] is not None:
        prepared["edls_missed"] = _write_edls(job["id"], prepared["edls"],
                                              cache.get("edls"))
    return True


def _remember(job_id: str, prepared: dict):
    """Depois do commit: o que foi preparado É o que está gravado."""
    cache = _section_hash.setdefault(job_id, {})
    cache["scalars"] = prepared["digests"]
    if prepared["candidates"] is not None:
        cache["candidates"] = prepared["candidates"][1]
    if prepared["episodes"] is not None:
        cache["episodes"] = {k: it[2] for k, it in prepared["episodes"].items()}
    if prepared["torrents"] is not None:
        cache["torrents"] = {n: (it[2], it[3])
                             for n, it in prepared["torrents"].items()}
    if prepared["edls"] is not None:
        # a EDL que ficou sem gravar fica sem hash: o próximo upsert a grava
        missed = set(prepared.get("edls_missed") or ())
        cache["edls"] = {k: it[0] for k, it in prepared["edls"].items()
                         if k not in missed}


def upsert_job(job: dict, create: bool = True):
    """Grava o job. Com create=False só ATUALIZA uma linha existente: é o que o
    flush atrasado de jobs sujos usa (jobs.runtime) — se o job foi removido no
    meio da janela, a gravação atrasada não pode recriá-lo (nem as linhas das
    tabelas filhas: sem a linha em `jobs`, nada é inserido).

    As seções em tabela própria só são reescritas quando mudaram desde a
    última gravação (hash por job; por linha nos episódios e torrents; por
    EDL nas EDLs)."""
    prepared = _prepare_upsert(job)
    with _lock:
        try:
            if not _apply_upsert(job, prepared, create):
                _conn.rollback()
                return
            _conn.commit()
        except BaseException:
            _conn.rollback()
            raise
        _remember(job["id"], prepared)


def _legacy_candidate_events(doc: dict, events: list[dict]
                             ) -> tuple[dict, list[tuple[int, dict]]]:
    """Eventos "candidates" do formato antigo (rastro inteiro em events.data)
    -> uma consulta + linhas de rastro no job, e o evento passa a só
    referenciá-la. Função pura: devolve (doc novo, [(id do evento, data
    nova)]). O candidato viável do rastro que já está na lista escolhível do
    papel (mesmo título/tracker) é a MESMA linha — só ganha a posição no
    rastro; os rejeitados viram linhas só de rastro."""
    job = {"search_queries": [dict(q) for q in doc.get("search_queries") or []],
           "search_candidates": [dict(c, episodes=dict(c.get("episodes") or {}))
                                 for c in doc.get("search_candidates") or []]}
    updates: list[tuple[int, dict]] = []
    for ev in events:
        d = ev.get("data")
        if (ev.get("kind") != "candidates" or not isinstance(d, dict)
                or not isinstance(d.get("candidates"), list)):
            continue
        role = d.get("role") or "?"
        n = cands.add_query(job, role, d.get("query") or "", None)
        job["search_queries"][-1]["ts"] = ev.get("ts")
        for i, t in enumerate(d["candidates"]):
            row = None
            if t.get("rejected") is None:
                row = next((c for c in cands.selectable(job, role)
                            if c["query"] is None and c["trace"] is None
                            and c["title"] == t.get("title")
                            and c["tracker"] == t.get("tracker")), None)
            if row is not None:
                row.update(query=n, trace=i)
                for k in ("quality", "rejected", "chosen", "year_match", "coverage"):
                    if row.get(k) is None and k in t:
                        row[k] = t[k]
            else:
                cands.rows(job).append(cands.new(role, f"q{n}.{i}", t, query=n, trace=i))
        data = {k: v for k, v in d.items() if k != "candidates"}
        data["search_query"] = n
        updates.append((ev["id"], data))
    return {**doc, **job}, updates


def migrate_candidate_events(job_id: str) -> int:
    """Leva os eventos "candidates" antigos de um job para as tabelas (ver
    _legacy_candidate_events), numa transação só com a gravação do job.
    Devolve quantos eventos mudaram. Para a migração final do banco; até lá a
    lupa mostra o evento antigo como está (lista inline)."""
    job = get_job(job_id)
    if job is None:
        return 0
    with _lock:
        rows = _conn.execute(
            "SELECT id, ts, kind, data FROM events WHERE job_id = ? "
            "AND kind = 'candidates' ORDER BY id", (job_id,)).fetchall()
    events = [{"id": r[0], "ts": r[1], "kind": r[2],
               "data": json.loads(r[3]) if r[3] else None} for r in rows]
    new_doc, updates = _legacy_candidate_events(job, events)
    if not updates:
        return 0
    prepared = _prepare_upsert(new_doc)
    with _lock:
        try:
            if not _apply_upsert(new_doc, prepared, create=False):
                _conn.rollback()
                return 0
            _conn.executemany(
                "UPDATE events SET data = ? WHERE id = ?",
                [(json.dumps(d, ensure_ascii=False), eid) for eid, d in updates])
            _conn.commit()
        except BaseException:
            _conn.rollback()
            raise
        _remember(job_id, prepared)
    return len(updates)


def add_event(job_id: str, ev: dict):
    with _lock:
        _conn.execute(
            "INSERT INTO events (job_id, ts, kind, message, data) VALUES (?, ?, ?, ?, ?)",
            (job_id, ev["ts"], ev["kind"], ev["message"],
             json.dumps(ev["data"], ensure_ascii=False) if "data" in ev else None))
        _conn.commit()


# seções que moram em tabela própria (o que `sections=` escolhe na leitura).
# "edls" é a pesada (centenas de KB num job de série): só o detalhe e o
# pipeline (jobs ativos) carregam — as listagens usam LIST_SECTIONS.
SECTIONS = ("candidates", "episodes", "torrents", "edls")
LIST_SECTIONS = ("candidates", "episodes", "torrents")


def _load_docs(rows: list, sections: bool | tuple[str, ...] = True) -> list[dict]:
    """Linhas de `jobs` (_JOB_SELECT) -> dicts dos jobs, com as seções em tabela
    (chamado com _lock). `sections`: True = todas, False = nenhuma, ou uma
    tupla de SECTIONS. Seção não lida fica FORA do dict — dict carregado sem
    alguma seção nunca deve ser regravado (o upsert não mexe nela, mas o
    documento é sobrescrito)."""
    names = SECTIONS if sections is True else tuple(sections or ())
    rows = [dict(zip(_JOB_SELECT_NAMES, r)) for r in rows]
    ids = [c["id"] for c in rows]
    kids = _load_scalar_kids([c for c in rows if c["keys"] is not None])
    loaded = {}
    if "candidates" in names:
        loaded["candidates"] = _load_candidates(ids)
    if "episodes" in names:
        loaded["episodes"] = _load_episodes(ids, legacy_edl="edls" in names)
    if "torrents" in names:
        loaded["torrents"] = _load_torrents(ids)
    if "edls" in names:
        loaded["edls"] = _load_edls(ids)
    return [_compose(_row_doc(c, kids.get(c["id"]) or {}),
                     {k: v.get(c["id"]) for k, v in loaded.items()},
                     legacy_scalars=c["keys"] is None)
            for c in rows]


def load_jobs() -> list[dict]:
    with _lock:
        rows = _conn.execute(_JOB_SELECT).fetchall()
        return _load_docs(rows)


def load_jobs_by_status(statuses: tuple[str, ...], limit: int | None = None,
                        offset: int = 0, media: str | None = None,
                        sections: bool | tuple[str, ...] = True) -> list[dict]:
    """Carrega os jobs cujo status está na lista (ex.: só os ativos, ou só os
    terminais). Usa a coluna `status` indexada em vez de ler tudo.

    Com `limit`, pagina no SQL (ordenado por created_at desc, o mesmo critério
    da tela) — assim uma biblioteca com milhares de jobs terminais não vira um
    JSON gigante a cada abertura da lista. `media` ("movie"/"tv") filtra pela
    dimensão de mídia direto no SQL. `sections` escolhe as seções em tabela
    que vêm junto (False = nenhuma; ex.: o resumo do cabeçalho só precisa dos
    torrents, para o % do download).
    """
    if not statuses:
        return []
    ph = ",".join("?" for _ in statuses)
    where = f"status IN ({ph})"
    params: list = list(statuses)
    if media is not None:
        # coluna (idx_jobs_media); job antigo sem a chave já é 'movie' nela
        where += " AND media_type = ?"
        params.append(media)
    if limit is None:
        where_j = where.replace("status", "j.status").replace(
            "media_type", "j.media_type")
        sql = f"{_JOB_SELECT} WHERE {where_j} ORDER BY j.created_at DESC"
    else:
        # a página é escolhida numa consulta só de ids: ordenar as linhas
        # largas (todas as colunas + o JOIN dos metadados) para descartar
        # quase todas custava mais que montar a página inteira
        sql = (f"{_JOB_SELECT} WHERE j.id IN (SELECT id FROM jobs WHERE "
               f"{where} ORDER BY created_at DESC LIMIT ? OFFSET ?) "
               f"ORDER BY j.created_at DESC")
        params += [limit, offset]
    with _lock:
        rows = _conn.execute(sql, params).fetchall()
        return _load_docs(rows, sections)


def get_job(job_id: str) -> dict | None:
    """Carrega um único job do banco (usado para jobs terminais que não ficam
    mais em memória)."""
    with _lock:
        row = _conn.execute(f"{_JOB_SELECT} WHERE j.id = ?",
                            (job_id,)).fetchone()
        return _load_docs([row])[0] if row else None


def count_jobs_by_status(media: str | None = None) -> dict[str, int]:
    """{status: quantidade} — para os badges de contagem sem baixar as listas.

    `media` ("movie"/"tv") restringe a contagem à dimensão de mídia."""
    sql = "SELECT status, COUNT(*) FROM jobs"
    params: list = []
    if media is not None:
        sql += " WHERE media_type = ?"
        params.append(media)
    sql += " GROUP BY status"
    with _lock:
        rows = _conn.execute(sql, params).fetchall()
    return {status: n for status, n in rows}


def error_jobs_for(tmdb_id: int, language: str, kind: str) -> list[str]:
    """IDs dos jobs em 'error' que casam tmdb_id + idioma + kind.

    Usado ao (re)adicionar um filme: um erro anterior daquela mesma variante é
    descartado (o novo job o substitui). Concluído/cancelado NÃO entram aqui —
    persistem mesmo com readição.
    """
    with _lock:
        # job sem a chave `kind` (anterior à opção) é "both"
        out = [r[0] for r in _conn.execute(
            "SELECT id FROM jobs WHERE status = 'error' AND keys IS NOT NULL "
            "AND tmdb_id = ? AND language = ? "
            "AND (CASE WHEN keys & ? THEN kind ELSE 'both' END) = ?",
            (tmdb_id, language, _MASK_BIT["kind"], kind))]
        # linhas do formato antigo: tudo ainda no documento
        rows = _conn.execute("SELECT id, data FROM jobs WHERE status = 'error' "
                             "AND keys IS NULL").fetchall()
    for jid, raw in rows:
        try:
            d = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if (d.get("tmdb_id") == tmdb_id and d.get("language") == language
                and d.get("kind", "both") == kind):
            out.append(jid)
    return out


def load_events(job_id: str) -> list[dict]:
    with _lock:
        rows = _conn.execute(
            "SELECT ts, kind, message, data FROM events WHERE job_id = ? "
            "ORDER BY id DESC LIMIT ?", (job_id, MAX_EVENTS_RETURNED)).fetchall()
    events = []
    for ts, kind, message, data in reversed(rows):
        ev = {"ts": ts, "kind": kind, "message": message}
        if data is not None:
            ev["data"] = json.loads(data)
        events.append(ev)
    return events


def delete_job(job_id: str):
    with _lock:
        _conn.execute("DELETE FROM events WHERE job_id = ?", (job_id,))
        # tabelas filhas (candidatos...) saem pelo ON DELETE CASCADE
        _conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
        _conn.commit()
        _section_hash.pop(job_id, None)


def _migrate_from_json():
    path = config.JOBS_FILE
    if not path.exists():
        return
    backup = path.parent / (path.name + ".bak")
    try:
        old_jobs = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        path.replace(backup)  # corrompido: preserva para inspecao e segue
        return
    with _lock:
        existing = {r[0] for r in _conn.execute("SELECT id FROM jobs").fetchall()}
    for job in old_jobs:
        if job.get("id") in existing:
            continue
        events = job.pop("events", None) or []
        upsert_job(job)
        for ev in events:
            add_event(job["id"], ev)
    path.replace(backup)
