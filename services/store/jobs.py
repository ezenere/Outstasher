"""Persistência do job: decomposição, gravação, recomposição e leitura.

upsert_job decompõe o dict nas seções (_prepare_upsert / _apply_upsert) e só
reescreve as que mudaram (hash em store._section_hash — _remember);
get_job / load_jobs* recompõem o mesmo dict a partir das tabelas (_compose,
normalizando o formato antigo); contagens, delete_job, a migração dos
eventos de candidatos antigos e a importação do jobs.json antigo.
"""
import json

import config
from services import store

from .candidates import (
    _LEGACY_KEYS, _SECTIONS, _is_legacy, _legacy_candidate_events,
    _legacy_candidates, _load_candidates, _rows_digest, _section_rows,
    _write_sections,
)
from .edls import (
    EDLS_UNLOADED, EDL_MOVIE_KEY, _edl_items, _edl_round_trip,
    _has_legacy_edls, _legacy_edls, _load_edls, _without_edls, _write_edls,
)
from .events import add_event
from .scalar_rows import (
    _JOB_SELECT, _JOB_SELECT_NAMES, _load_scalar_kids, _row_doc,
    _write_scalar_rows,
)
from .scalars import (
    _JCOLS, _MASK_BIT, _ROW_KEYS, _encode, _legacy_scalars, _scalar_digests,
)
from .series import (
    _SERIES_KEYS, _episode_items, _legacy_series, _load_episodes,
    _load_torrents, _torrent_items, _write_episodes, _write_torrents,
)


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
    cache = store._section_hash.setdefault(jid, {})
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
        store._section_hash.pop(jid, None)
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
        store._conn.execute(
            f"INSERT INTO jobs (id, created_at, status, data, {names}) "
            f"VALUES ({ph}) ON CONFLICT(id) DO UPDATE SET {upd}",
            (job["id"], job["created_at"], job["status"], data, *vals))
    else:
        upd = ", ".join(f"{c} = ?" for c in ("status", "data") + _JCOLS)
        cur = store._conn.execute(f"UPDATE jobs SET {upd} WHERE id = ?",
                            (job["status"], data, *vals, job["id"]))
        if cur.rowcount == 0:
            return False
    cache = store._section_hash.get(job["id"]) or {}
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
    cache = store._section_hash.setdefault(job_id, {})
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
    with store._lock:
        try:
            if not _apply_upsert(job, prepared, create):
                store._conn.rollback()
                return
            store._conn.commit()
        except BaseException:
            store._conn.rollback()
            raise
        _remember(job["id"], prepared)


def migrate_candidate_events(job_id: str) -> int:
    """Leva os eventos "candidates" antigos de um job para as tabelas (ver
    _legacy_candidate_events), numa transação só com a gravação do job.
    Devolve quantos eventos mudaram. Para a migração final do banco; até lá a
    lupa mostra o evento antigo como está (lista inline)."""
    job = get_job(job_id)
    if job is None:
        return 0
    with store._lock:
        rows = store._conn.execute(
            "SELECT id, ts, kind, data FROM events WHERE job_id = ? "
            "AND kind = 'candidates' ORDER BY id", (job_id,)).fetchall()
    events = [{"id": r[0], "ts": r[1], "kind": r[2],
               "data": json.loads(r[3]) if r[3] else None} for r in rows]
    new_doc, updates = _legacy_candidate_events(job, events)
    if not updates:
        return 0
    prepared = _prepare_upsert(new_doc)
    with store._lock:
        try:
            if not _apply_upsert(new_doc, prepared, create=False):
                store._conn.rollback()
                return 0
            store._conn.executemany(
                "UPDATE events SET data = ? WHERE id = ?",
                [(json.dumps(d, ensure_ascii=False), eid) for eid, d in updates])
            store._conn.commit()
        except BaseException:
            store._conn.rollback()
            raise
        _remember(job_id, prepared)
    return len(updates)


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
    with store._lock:
        rows = store._conn.execute(_JOB_SELECT).fetchall()
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
    with store._lock:
        rows = store._conn.execute(sql, params).fetchall()
        return _load_docs(rows, sections)


def get_job(job_id: str) -> dict | None:
    """Carrega um único job do banco (usado para jobs terminais que não ficam
    mais em memória)."""
    with store._lock:
        row = store._conn.execute(f"{_JOB_SELECT} WHERE j.id = ?",
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
    with store._lock:
        rows = store._conn.execute(sql, params).fetchall()
    return {status: n for status, n in rows}


def error_jobs_for(tmdb_id: int, language: str, kind: str) -> list[str]:
    """IDs dos jobs em 'error' que casam tmdb_id + idioma + kind.

    Usado ao (re)adicionar um filme: um erro anterior daquela mesma variante é
    descartado (o novo job o substitui). Concluído/cancelado NÃO entram aqui —
    persistem mesmo com readição.
    """
    with store._lock:
        # job sem a chave `kind` (anterior à opção) é "both"
        out = [r[0] for r in store._conn.execute(
            "SELECT id FROM jobs WHERE status = 'error' AND keys IS NOT NULL "
            "AND tmdb_id = ? AND language = ? "
            "AND (CASE WHEN keys & ? THEN kind ELSE 'both' END) = ?",
            (tmdb_id, language, _MASK_BIT["kind"], kind))]
        # linhas do formato antigo: tudo ainda no documento
        rows = store._conn.execute("SELECT id, data FROM jobs WHERE status = 'error' "
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


def delete_job(job_id: str):
    with store._lock:
        store._conn.execute("DELETE FROM events WHERE job_id = ?", (job_id,))
        # tabelas filhas (candidatos...) saem pelo ON DELETE CASCADE
        store._conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
        store._conn.commit()
        store._section_hash.pop(job_id, None)


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
    with store._lock:
        existing = {r[0] for r in store._conn.execute("SELECT id FROM jobs").fetchall()}
    for job in old_jobs:
        if job.get("id") in existing:
            continue
        events = job.pop("events", None) or []
        upsert_job(job)
        for ev in events:
            add_event(job["id"], ev)
    path.replace(backup)
