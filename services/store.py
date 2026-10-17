"""Persistencia em SQLite (jobs.db).

- jobs: um documento JSON por job (upsert pequeno a cada mudanca), com as
  listas padronizadas fora dele, em tabelas próprias (ver upsert_job):
  search_queries / search_candidates / candidate_episodes (candidatos) e,
  nos jobs de série, job_episodes / episode_subs / job_torrents /
  torrent_coverage / torrent_files; as EDLs do alinhador (série e filme) em
  edls / edl_segments.
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
    # jobs antigos não têm media_type no JSON: IFNULL trata como filme. O índice
    # usa a MESMA expressão das queries para o SQLite conseguir aproveitá-lo.
    _conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_jobs_media "
        "ON jobs(IFNULL(json_extract(data, '$.media_type'), 'movie'))")
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
    # idiomas da faixa dublada (editaveis pela UI). markers_* guardados como JSON.
    _conn.execute("""
        CREATE TABLE IF NOT EXISTS languages (
            code TEXT PRIMARY KEY,
            label TEXT NOT NULL,
            tmdb TEXT NOT NULL,
            markers_strong TEXT NOT NULL DEFAULT '[]',
            markers_weak TEXT NOT NULL DEFAULT '[]',
            sort_order INTEGER NOT NULL DEFAULT 0
        )""")
    _create_candidate_tables()
    _create_series_tables()
    _create_edl_tables()
    _conn.commit()
    _migrate_from_json()
    _seed_default_destination()
    _seed_default_torrent_target()
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
# Guardadas como um único JSON em settings. Formato:
#   { "<lang>": { "no_year": ["indexerId", ...], "roman": [...], "roman_no_year": [...] } }
# lang = código do idioma (pt/es/...); as chaves de variante são fixas.
_EXTRA_SEARCH_KEY = "extra_search_rules"
EXTRA_SEARCH_VARIANTS = ("no_year", "roman", "roman_no_year")


def get_extra_search_rules() -> dict:
    raw = get_setting(_EXTRA_SEARCH_KEY)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def set_extra_search_rules(rules: dict):
    set_setting(_EXTRA_SEARCH_KEY, json.dumps(rules, ensure_ascii=False))


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

def _lang_row(r) -> dict:
    return {"code": r[0], "label": r[1], "tmdb": r[2],
            "markers_strong": json.loads(r[3] or "[]"),
            "markers_weak": json.loads(r[4] or "[]"),
            "sort_order": r[5]}


def _seed_languages():
    """Na primeira execução, popula a tabela com os idiomas padrão do config."""
    with _lock:
        count = _conn.execute("SELECT COUNT(*) FROM languages").fetchone()[0]
    if count:
        return
    for i, (code, info) in enumerate(config._DEFAULT_LANGUAGES.items()):
        with _lock:
            _conn.execute(
                "INSERT INTO languages (code, label, tmdb, markers_strong, "
                "markers_weak, sort_order) VALUES (?, ?, ?, ?, ?, ?)",
                (code, info["label"], info["tmdb"],
                 json.dumps(info["markers_strong"], ensure_ascii=False),
                 json.dumps(info["markers_weak"], ensure_ascii=False), i))
            _conn.commit()
    # marcadores de legenda: settings (lista unica, nao por idioma)
    if get_setting("subtitle_markers") is None:
        set_setting("subtitle_markers",
                    json.dumps(config._DEFAULT_SUBTITLE_MARKERS, ensure_ascii=False))


def list_languages() -> list[dict]:
    with _lock:
        rows = _conn.execute(
            "SELECT code, label, tmdb, markers_strong, markers_weak, sort_order "
            "FROM languages ORDER BY sort_order, code").fetchall()
    return [_lang_row(r) for r in rows]


def get_subtitle_markers() -> list[str]:
    raw = get_setting("subtitle_markers")
    if not raw:
        return list(config._DEFAULT_SUBTITLE_MARKERS)
    try:
        val = json.loads(raw)
        return [str(m) for m in val] if isinstance(val, list) else []
    except (ValueError, TypeError):
        return []


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
        _conn.execute("DELETE FROM languages")
        for i, lg in enumerate(languages):
            _conn.execute(
                "INSERT INTO languages (code, label, tmdb, markers_strong, "
                "markers_weak, sort_order) VALUES (?, ?, ?, ?, ?, ?)",
                (lg["code"], lg["label"], lg["tmdb"],
                 json.dumps(lg["markers_strong"], ensure_ascii=False),
                 json.dumps(lg["markers_weak"], ensure_ascii=False), i))
        _conn.commit()
    set_setting("subtitle_markers", json.dumps(subtitle_markers, ensure_ascii=False))
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
#             "edls": {ep_key | "filme": hash da EDL}}}
# Candidatos mudam em bloco e a seção inteira é reescrita; episódios,
# torrents e EDLs mudam um de cada vez (o merge conclui UM episódio, o
# watchdog atualiza o progresso de UM torrent, a revisão decide a EDL de UM
# episódio), então o hash é por linha e só a linha que mudou é regravada. Sem entrada para a seção (processo novo, ou o job
# veio do formato antigo), o próximo upsert reescreve a seção inteira.
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


def _episode_from_row(row: tuple, subs) -> tuple[dict, list | None]:
    """Linha de job_episodes (+ legendas) -> (episódio, [s, e] alternativo)."""
    v = dict(zip(_EP_COLS, row))
    extra = json.loads(v["extra"]) if v["extra"] else {}
    src = {k: v[f"src_{k}"] for k in _SRC_KEYS if v[f"src_{k}"] is not None}
    src.update(extra.pop("_src", None) or {})
    ep = {k: v[k] for k in _EP_PLAIN}
    ep["src"] = src
    for role, _pos, path in subs or ():
        ep.setdefault("subs", {}).setdefault(role, []).append(path)
    if v["rel"] is not None:
        ep["rel"] = v["rel"]
    ep.update(extra)
    alt = ([v["alt_season"], v["alt_episode"]]
           if v["alt_season"] is not None else None)
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
        for r in _conn.execute(
                f"SELECT job_id, ep_key, {cols} FROM job_episodes "
                f"WHERE job_id IN ({ph}) ORDER BY job_id, pos", chunk):
            eps, om, old = out.setdefault(r[0], ({}, {}, {}))
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


# -------------------- composição / decomposição do job --------------------

def _compose(doc: dict, loaded: dict) -> dict:
    """Documento de jobs.data + linhas das tabelas -> o dict do job.

    `loaded` traz só as seções pedidas ({"candidates"|"episodes"|"torrents"|
    "edls": linhas do job ou None}); seção não pedida fica fora do dict (sem
    as EDLs, o dict leva a marca EDLS_UNLOADED). Documento no formato antigo
    (seção ainda dentro de jobs.data, ou a EDL na coluna provisória de
    job_episodes) é normalizado: gravado antes das tabelas, as linhas ainda
    não existem, e o próximo upsert (job ativo) grava no formato novo."""
    jid = doc["id"]
    cache = _section_hash.setdefault(jid, {})
    with_edls = "edls" in loaded
    if _is_legacy(doc):
        doc = _legacy_candidates(doc)
        cache.pop("candidates", None)
    elif "candidates" in loaded:
        doc.update(loaded["candidates"]
                   or {"search_queries": [], "search_candidates": []})
        # o que acabou de ser lido É o que está gravado: o próximo upsert sem
        # mudança nos candidatos não reescreve as linhas
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
    skip = {"events", EDLS_UNLOADED, *_SECTIONS, *_LEGACY_KEYS,
            *(_SERIES_KEYS if tv else ())}
    doc = {k: v for k, v in job.items() if k not in skip}
    adv = doc.get("advanced")
    if not tv and isinstance(adv, dict) and "edl" in adv:
        # filme: a EDL vai para as tabelas; os dois arquivos ficam no documento
        doc["advanced"] = {k: v for k, v in adv.items() if k != "edl"}
    gate = doc.get("awaiting")
    if (isinstance(gate, dict) and gate.get("reason") == "alignment_review"
            and gate.get("payload") is not None):
        # cópia das EDLs (dict vindo de fora do fluxo novo, ex. jobs.json
        # antigo): o payload desse gate é sempre derivado, nunca gravado
        doc["awaiting"] = {**gate, "payload": None}
    prepared: dict = {"data": json.dumps(doc, ensure_ascii=False),
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
    if create:
        _conn.execute(
            "INSERT INTO jobs (id, created_at, status, data) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET status = excluded.status, data = excluded.data",
            (job["id"], job["created_at"], job["status"], data))
    else:
        cur = _conn.execute("UPDATE jobs SET status = ?, data = ? WHERE id = ?",
                            (job["status"], data, job["id"]))
        if cur.rowcount == 0:
            return False
    cache = _section_hash.get(job["id"]) or {}
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
    """Linhas (id, data) de `jobs` -> dicts dos jobs, com as seções em tabela
    (chamado com _lock). `sections`: True = todas, False = nenhuma, ou uma
    tupla de SECTIONS. Seção não lida fica FORA do dict — dict carregado sem
    alguma seção nunca deve ser regravado (o upsert não mexe nela, mas o
    documento é sobrescrito)."""
    names = SECTIONS if sections is True else tuple(sections or ())
    ids = [r[0] for r in rows]
    loaded = {}
    if "candidates" in names:
        loaded["candidates"] = _load_candidates(ids)
    if "episodes" in names:
        loaded["episodes"] = _load_episodes(ids, legacy_edl="edls" in names)
    if "torrents" in names:
        loaded["torrents"] = _load_torrents(ids)
    if "edls" in names:
        loaded["edls"] = _load_edls(ids)
    return [_compose(json.loads(r[1]), {k: v.get(r[0]) for k, v in loaded.items()})
            for r in rows]


def load_jobs() -> list[dict]:
    with _lock:
        rows = _conn.execute("SELECT id, data FROM jobs").fetchall()
        return _load_docs(rows)


# mesma expressão do índice idx_jobs_media (jobs antigos, sem a chave, = filme)
_MEDIA_EXPR = "IFNULL(json_extract(data, '$.media_type'), 'movie')"


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
    sql = f"SELECT id, data FROM jobs WHERE status IN ({ph})"
    params: list = list(statuses)
    if media is not None:
        sql += f" AND {_MEDIA_EXPR} = ?"
        params.append(media)
    sql += " ORDER BY created_at DESC"
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        params += [limit, offset]
    with _lock:
        rows = _conn.execute(sql, params).fetchall()
        return _load_docs(rows, sections)


def get_job(job_id: str) -> dict | None:
    """Carrega um único job do banco (usado para jobs terminais que não ficam
    mais em memória)."""
    with _lock:
        row = _conn.execute(
            "SELECT id, data FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return _load_docs([row])[0] if row else None


def count_jobs_by_status(media: str | None = None) -> dict[str, int]:
    """{status: quantidade} — para os badges de contagem sem baixar as listas.

    `media` ("movie"/"tv") restringe a contagem à dimensão de mídia."""
    sql = "SELECT status, COUNT(*) FROM jobs"
    params: list = []
    if media is not None:
        sql += f" WHERE {_MEDIA_EXPR} = ?"
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
        rows = _conn.execute(
            "SELECT id, data FROM jobs WHERE status = 'error'").fetchall()
    out = []
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
