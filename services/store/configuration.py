"""Configuração persistida.

settings (chave/valor), regras de busca extra (search_rules), presets de
conversão, destinos do arquivo final, destinos dos torrents (qBittorrent),
idiomas da faixa dublada com os marcadores (language_markers) e marcadores de
legenda (subtitle_markers) — com a migração única das listas do formato
antigo (JSON em settings / languages.markers_*) e os seeds da primeira
execução.
"""
import json

import config
from services import store


# -------------------- settings (chave/valor) --------------------

def get_setting(key: str, default: str | None = None) -> str | None:
    with store._lock:
        r = store._conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return r[0] if r else default


def set_setting(key: str, value: str):
    with store._lock:
        store._conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value))
        store._conn.commit()


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
    store._conn.execute("DELETE FROM search_rules")
    store._conn.executemany(
        "INSERT INTO search_rules (pos, language, rule, tracker) "
        "VALUES (?, ?, ?, ?)", [(i, *r) for i, r in enumerate(rows)])


def get_extra_search_rules() -> dict:
    with store._lock:
        rows = store._conn.execute("SELECT language, rule, tracker FROM search_rules "
                             "ORDER BY pos").fetchall()
    out: dict = {}
    for lang, rule, tracker in rows:
        out.setdefault(lang, {}).setdefault(rule, []).append(tracker)
    return out


def set_extra_search_rules(rules: dict):
    with store._lock:
        _write_search_rules(_search_rule_rows(rules))
        store._conn.commit()


# -------------------- presets de conversao --------------------
# ConvertOptions nomeadas ("QSV AV1 CRF 20"). Guardadas como JSON opaco: quem
# valida e o transcode.validate() na hora de converter, entao um preset salvo
# antes de uma mudanca de capabilities nao quebra a listagem.

def list_convert_presets() -> list[dict]:
    with store._lock:
        rows = store._conn.execute(
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
    with store._lock:
        store._conn.execute(
            "INSERT INTO convert_presets (name, options) VALUES (?, ?) "
            "ON CONFLICT(name) DO UPDATE SET options = excluded.options",
            (name, payload))
        store._conn.commit()
        r = store._conn.execute(
            "SELECT id, name, options FROM convert_presets WHERE name = ?",
            (name,)).fetchone()
    return {"id": r[0], "name": r[1], "options": _json_or_none(r[2])}


def delete_convert_preset(preset_id: int) -> bool:
    with store._lock:
        cur = store._conn.execute("DELETE FROM convert_presets WHERE id = ?", (preset_id,))
        store._conn.commit()
        return cur.rowcount > 0


# -------------------- destinos (pastas de destino do arquivo final) --------------------

def list_destinations() -> list[dict]:
    with store._lock:
        rows = store._conn.execute(
            "SELECT id, label, path, media, is_default FROM destinations "
            "ORDER BY is_default DESC, label COLLATE NOCASE").fetchall()
    return [_dest_row(r) for r in rows]


def _dest_row(r) -> dict:
    return {"id": r[0], "label": r[1], "path": r[2], "media": r[3],
            "is_default": bool(r[4])}


def list_destinations_by_media(media: str) -> list[dict]:
    """Destinos de UMA mídia ('movie'/'tv') — filmes e séries têm bibliotecas
    separadas; cada fluxo só enxerga os destinos da própria mídia."""
    with store._lock:
        rows = store._conn.execute(
            "SELECT id, label, path, media, is_default FROM destinations "
            "WHERE media = ? ORDER BY is_default DESC, label COLLATE NOCASE",
            (media,)).fetchall()
    return [_dest_row(r) for r in rows]


def get_destination(dest_id: int) -> dict | None:
    with store._lock:
        r = store._conn.execute(
            "SELECT id, label, path, media, is_default FROM destinations WHERE id = ?",
            (dest_id,)).fetchone()
    return _dest_row(r) if r else None


def default_destination(media: str = "movie") -> dict | None:
    """Destino padrão DA MÍDIA (o is_default é por mídia)."""
    with store._lock:
        r = store._conn.execute(
            "SELECT id, label, path, media, is_default FROM destinations "
            "WHERE media = ? ORDER BY is_default DESC, id LIMIT 1",
            (media,)).fetchone()
    return _dest_row(r) if r else None


def add_destination(label: str, path: str, is_default: bool = False,
                    media: str = "movie") -> dict:
    from datetime import datetime
    with store._lock:
        if is_default:
            # o padrão é POR mídia: marcar um destino de série como padrão não
            # desmarca o padrão dos filmes
            store._conn.execute("UPDATE destinations SET is_default = 0 WHERE media = ?",
                          (media,))
        cur = store._conn.execute(
            "INSERT INTO destinations (label, path, media, is_default, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (label, path, media, 1 if is_default else 0,
             datetime.now().isoformat(timespec="seconds")))
        store._conn.commit()
        dest_id = cur.lastrowid
    return get_destination(dest_id)


def update_destination(dest_id: int, label: str, path: str, is_default: bool,
                       media: str | None = None) -> dict | None:
    with store._lock:
        row = store._conn.execute(
            "SELECT media FROM destinations WHERE id = ?", (dest_id,)).fetchone()
        if not row:
            return None
        media = media or row[0]
        if is_default:
            store._conn.execute("UPDATE destinations SET is_default = 0 WHERE media = ?",
                          (media,))
        store._conn.execute(
            "UPDATE destinations SET label = ?, path = ?, media = ?, "
            "is_default = ? WHERE id = ?",
            (label, path, media, 1 if is_default else 0, dest_id))
        store._conn.commit()
    return get_destination(dest_id)


def delete_destination(dest_id: int) -> bool:
    with store._lock:
        cur = store._conn.execute("DELETE FROM destinations WHERE id = ?", (dest_id,))
        store._conn.commit()
        return cur.rowcount > 0


def _seed_default_destination():
    """Na primeira vez, cria um destino a partir do OUTPUT_DIR do .env (se houver)."""
    with store._lock:
        count = store._conn.execute("SELECT COUNT(*) FROM destinations").fetchone()[0]
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
    with store._lock:
        rows = store._conn.execute(
            "SELECT id, label, save_path, local_path, is_default FROM torrent_targets "
            "ORDER BY is_default DESC, label COLLATE NOCASE").fetchall()
    return [_target_row(r) for r in rows]


def get_torrent_target(target_id: int) -> dict | None:
    with store._lock:
        r = store._conn.execute(
            "SELECT id, label, save_path, local_path, is_default FROM torrent_targets "
            "WHERE id = ?", (target_id,)).fetchone()
    return _target_row(r) if r else None


def default_torrent_target() -> dict | None:
    with store._lock:
        r = store._conn.execute(
            "SELECT id, label, save_path, local_path, is_default FROM torrent_targets "
            "ORDER BY is_default DESC, id LIMIT 1").fetchone()
    return _target_row(r) if r else None


def add_torrent_target(label: str, save_path: str, local_path: str,
                       is_default: bool = False) -> dict:
    from datetime import datetime
    with store._lock:
        if is_default:
            store._conn.execute("UPDATE torrent_targets SET is_default = 0")
        cur = store._conn.execute(
            "INSERT INTO torrent_targets (label, save_path, local_path, is_default, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (label, save_path, local_path, 1 if is_default else 0,
             datetime.now().isoformat(timespec="seconds")))
        store._conn.commit()
        target_id = cur.lastrowid
    return get_torrent_target(target_id)


def update_torrent_target(target_id: int, label: str, save_path: str,
                          local_path: str, is_default: bool) -> dict | None:
    with store._lock:
        if not store._conn.execute("SELECT 1 FROM torrent_targets WHERE id = ?",
                             (target_id,)).fetchone():
            return None
        if is_default:
            store._conn.execute("UPDATE torrent_targets SET is_default = 0")
        store._conn.execute(
            "UPDATE torrent_targets SET label = ?, save_path = ?, local_path = ?, "
            "is_default = ? WHERE id = ?",
            (label, save_path, local_path, 1 if is_default else 0, target_id))
        store._conn.commit()
    return get_torrent_target(target_id)


def delete_torrent_target(target_id: int) -> bool:
    with store._lock:
        cur = store._conn.execute("DELETE FROM torrent_targets WHERE id = ?", (target_id,))
        store._conn.commit()
        return cur.rowcount > 0


def _seed_default_torrent_target():
    """Na primeira vez, cria um target a partir do QBIT_SAVE_PATH/QBIT_PATH_MAP do .env."""
    with store._lock:
        count = store._conn.execute("SELECT COUNT(*) FROM torrent_targets").fetchone()[0]
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
    store._conn.execute("""
        CREATE TABLE IF NOT EXISTS language_markers (
            language TEXT NOT NULL REFERENCES languages(code) ON DELETE CASCADE,
            strength TEXT NOT NULL CHECK (strength IN ('strong', 'weak')),
            pos INTEGER NOT NULL,
            marker TEXT NOT NULL,
            PRIMARY KEY (language, strength, pos)
        )""")
    store._conn.execute("""
        CREATE TABLE IF NOT EXISTS subtitle_markers (
            pos INTEGER PRIMARY KEY,
            marker TEXT NOT NULL
        )""")
    # sem FK para languages: as regras de um idioma sobrevivem a regravar a
    # tabela de idiomas (save_languages apaga e reinsere todos), como no JSON
    store._conn.execute("""
        CREATE TABLE IF NOT EXISTS search_rules (
            pos INTEGER PRIMARY KEY,
            language TEXT NOT NULL,
            rule TEXT NOT NULL,
            tracker TEXT NOT NULL
        )""")
    store._conn.execute("CREATE INDEX IF NOT EXISTS idx_search_rules_lang "
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
    store._conn.executemany(
        "INSERT INTO language_markers (language, strength, pos, marker) "
        "VALUES (?, ?, ?, ?)",
        [(code, strength, i, m) for strength, key in _STRENGTHS
         for i, m in enumerate(lg.get(key) or [])])


def _write_subtitle_markers(markers: list[str]):
    store._conn.execute("DELETE FROM subtitle_markers")
    store._conn.executemany("INSERT INTO subtitle_markers (pos, marker) VALUES (?, ?)",
                      list(enumerate(markers)))


def _migrate_config_lists():
    """Banco anterior às tabelas: lê UMA vez os JSON antigos (markers_* de
    languages, settings.subtitle_markers, settings.extra_search_rules) e grava
    as linhas. Tudo numa transação, com a marca junto: rodar de novo (init()
    outra vez, restart) não duplica nem volta a ler o JSON — que fica no banco
    sem uso. Banco novo (sem a chave de legenda): os marcadores padrão."""
    with store._lock:
        if store._conn.execute("SELECT 1 FROM settings WHERE key = ?",
                         (_CONFIG_LISTS_FLAG,)).fetchone():
            return
        try:
            langs = store._conn.execute(
                "SELECT code, markers_strong, markers_weak FROM languages"
            ).fetchall()
            store._conn.execute("DELETE FROM language_markers")
            for code, strong, weak in langs:
                _write_language_markers(code, {
                    "markers_strong": _legacy_markers(strong),
                    "markers_weak": _legacy_markers(weak)})
            r = store._conn.execute("SELECT value FROM settings WHERE key = ?",
                              ("subtitle_markers",)).fetchone()
            # chave vazia/ausente: a leitura antiga caía nos padrões
            _write_subtitle_markers(
                _legacy_markers(r[0]) if r and r[0]
                else list(config._DEFAULT_SUBTITLE_MARKERS))
            r = store._conn.execute("SELECT value FROM settings WHERE key = ?",
                              (_EXTRA_SEARCH_KEY,)).fetchone()
            _write_search_rules(_legacy_search_rules(r[0] if r else None))
            store._conn.execute("INSERT INTO settings (key, value) VALUES (?, '1')",
                          (_CONFIG_LISTS_FLAG,))
            store._conn.commit()
        except BaseException:
            store._conn.rollback()
            raise


def _seed_languages():
    """Na primeira execução, popula a tabela com os idiomas padrão do config."""
    with store._lock:
        count = store._conn.execute("SELECT COUNT(*) FROM languages").fetchone()[0]
        if count:
            return
        for i, (code, info) in enumerate(config._DEFAULT_LANGUAGES.items()):
            store._conn.execute(
                "INSERT INTO languages (code, label, tmdb, sort_order) "
                "VALUES (?, ?, ?, ?)", (code, info["label"], info["tmdb"], i))
            _write_language_markers(code, info)
        store._conn.commit()


def list_languages() -> list[dict]:
    with store._lock:
        rows = store._conn.execute(
            "SELECT code, label, tmdb, sort_order FROM languages "
            "ORDER BY sort_order, code").fetchall()
        marks = store._conn.execute(
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
    with store._lock:
        rows = store._conn.execute(
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
    with store._lock:
        try:
            # o CASCADE já leva os marcadores; o DELETE explícito não depende
            # do PRAGMA foreign_keys
            store._conn.execute("DELETE FROM language_markers")
            store._conn.execute("DELETE FROM languages")
            for i, lg in enumerate(languages):
                store._conn.execute(
                    "INSERT INTO languages (code, label, tmdb, sort_order) "
                    "VALUES (?, ?, ?, ?)", (lg["code"], lg["label"], lg["tmdb"], i))
                _write_language_markers(lg["code"], lg)
            _write_subtitle_markers(list(subtitle_markers))
            store._conn.commit()
        except BaseException:
            store._conn.rollback()
            raise
    load_language_config()
