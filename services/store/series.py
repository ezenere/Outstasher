"""Episódios e torrents dos jobs de série.

Tabelas job_episodes / episode_subs / job_torrents / torrent_coverage /
torrent_files, a conversão linha <-> dict (por linha, com hash próprio) e a
normalização do documento antigo (_legacy_series).
"""
import json

from services import store

from .candidates import _CAND_COLS
from .common import _digest, _in_chunks, _json
from .edls import _edl_round_trip


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
    store._conn.execute("""
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
    store._conn.execute("""
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
    store._conn.execute("""
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
    store._conn.execute("CREATE INDEX IF NOT EXISTS idx_job_torrents_cand "
                  "ON job_torrents(job_id, cid)")
    # pos NULL = o episódio saiu da cobertura (só está ali pela flag shrunk:
    # o pack não tinha o arquivo — ver pipeline._ensure_file_selection)
    store._conn.execute("""
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
    store._conn.execute("""
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
        stored = [r[0] for r in store._conn.execute(
            "SELECT ep_key FROM job_episodes WHERE job_id = ?", (job_id,))]
        gone = [(job_id, k) for k in stored if k not in items]
        store._conn.execute("DELETE FROM episode_subs WHERE job_id = ?", (job_id,))
    else:
        gone = [(job_id, k) for k in cached if k not in items]
    if gone:
        store._conn.executemany(
            "DELETE FROM job_episodes WHERE job_id = ? AND ep_key = ?", gone)
    changed = (list(items) if cached is None
               else [k for k, it in items.items() if cached.get(k) != it[2]])
    if not changed:
        return
    cols = ", ".join(_EP_COLS)
    ph = ", ".join("?" for _ in range(len(_EP_COLS) + 2))
    upd = ", ".join(f"{c} = excluded.{c}" for c in _EP_COLS)
    store._conn.executemany(
        f"INSERT INTO job_episodes (job_id, ep_key, {cols}) VALUES ({ph}) "
        f"ON CONFLICT(job_id, ep_key) DO UPDATE SET {upd}",
        [(job_id, k) + items[k][0] for k in changed])
    if cached is not None:
        store._conn.executemany(
            "DELETE FROM episode_subs WHERE job_id = ? AND ep_key = ?",
            [(job_id, k) for k in changed])
    store._conn.executemany(
        "INSERT INTO episode_subs (job_id, ep_key, role, pos, path) "
        "VALUES (?, ?, ?, ?, ?)",
        [(job_id, k) + s for k in changed for s in items[k][1]])


def _write_torrents(job_id: str, items: dict, cached: dict | None):
    """Grava os torrents que mudaram: a linha (estado, hash, progresso) e os
    filhos (cobertura, arquivos selecionados) têm hashes separados. Upsert
    com ON CONFLICT DO UPDATE — INSERT OR REPLACE apagaria a linha e, pelo
    CASCADE, os filhos junto."""
    if cached is None:
        store._conn.execute("DELETE FROM job_torrents WHERE job_id = ?", (job_id,))
        rows, kids = list(items), list(items)
    else:
        gone = [(job_id, n) for n in cached if n not in items]
        if gone:
            store._conn.executemany(
                "DELETE FROM job_torrents WHERE job_id = ? AND n = ?", gone)
        old = {n: cached.get(n, (None, None)) for n in items}
        rows = [n for n, it in items.items() if old[n][0] != it[2]]
        kids = [n for n, it in items.items() if old[n][1] != it[3]]
    if rows:
        cols = ", ".join(_TOR_COLS)
        ph = ", ".join("?" for _ in range(len(_TOR_COLS) + 2))
        upd = ", ".join(f"{c} = excluded.{c}" for c in _TOR_COLS)
        store._conn.executemany(
            f"INSERT INTO job_torrents (job_id, n, {cols}) VALUES ({ph}) "
            f"ON CONFLICT(job_id, n) DO UPDATE SET {upd}",
            [(job_id, n) + items[n][0] for n in rows])
    if not kids:
        return
    if cached is not None:
        keys = [(job_id, n) for n in kids]
        store._conn.executemany(
            "DELETE FROM torrent_coverage WHERE job_id = ? AND n = ?", keys)
        store._conn.executemany(
            "DELETE FROM torrent_files WHERE job_id = ? AND n = ?", keys)
    store._conn.executemany(
        "INSERT INTO torrent_coverage (job_id, n, ep_key, pos, shrunk) "
        "VALUES (?, ?, ?, ?, ?)",
        [(job_id, n) + c for n in kids for c in items[n][1][0]])
    store._conn.executemany(
        "INSERT INTO torrent_files (job_id, n, pos, path) VALUES (?, ?, ?, ?)",
        [(job_id, n) + f for n in kids for f in items[n][1][1]])


def _load_episodes(job_ids: list[str], legacy_edl: bool = False
                   ) -> dict[str, tuple[dict, dict | None, dict]]:
    """{job_id: (episodes, order_map, EDLs da coluna legada)} lidos das
    tabelas (com _lock). A coluna legada `edl` só é lida com `legacy_edl`
    (quem carrega as EDLs): a listagem não paga o JSON dela."""
    out: dict[str, tuple[dict, dict | None, dict]] = {}
    cols = ", ".join(_EP_COLS) + (", edl" if legacy_edl else ", NULL")
    for chunk, ph in _in_chunks(job_ids):
        subs: dict[tuple, list] = {}
        for jid, key, role, pos, path in store._conn.execute(
                f"SELECT job_id, ep_key, role, pos, path FROM episode_subs "
                f"WHERE job_id IN ({ph}) ORDER BY job_id, ep_key, rowid", chunk):
            subs.setdefault((jid, key), []).append((role, pos, path))
        cur_id = None
        for r in store._conn.execute(
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
        for jid, n, key, pos, shrunk in store._conn.execute(
                f"SELECT job_id, n, ep_key, pos, shrunk FROM torrent_coverage "
                f"WHERE job_id IN ({ph})", chunk):
            kids.setdefault((jid, n), ([], []))[0].append((key, pos, shrunk))
        for jid, n, pos, path in store._conn.execute(
                f"SELECT job_id, n, pos, path FROM torrent_files "
                f"WHERE job_id IN ({ph}) ORDER BY job_id, n, pos", chunk):
            kids.setdefault((jid, n), ([], []))[1].append((pos, path))
        for r in store._conn.execute(
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
