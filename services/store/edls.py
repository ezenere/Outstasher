"""EDLs do alinhador (série e filme).

Tabelas edls / edl_segments, o perfil de confiança em BLOB, a conversão com
round-trip exato (spill), o hash por EDL e as normalizações do formato
antigo (EDL do filme dentro do documento, cópia no payload do gate de
revisão — _legacy_edls).
"""
import hashlib
import json
from array import array

import numpy as np

from services import store

from .common import _in_chunks, _json


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
    store._conn.execute("""
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
    store._conn.execute("""
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
    cached = (store._section_hash.get(job["id"]) or {}).get("edls") or {}
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
        gone = [r[0] for r in store._conn.execute(
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
        store._conn.executemany("DELETE FROM edls WHERE job_id = ? AND ep_key = ?",
                          [(job_id, k) for k in stale])
    cols = ", ".join(_EDL_COLS)
    ph = ", ".join("?" for _ in range(len(_EDL_COLS) + 4))
    seg_cols = ", ".join(_SEG_COLS)
    seg_ph = ", ".join("?" for _ in range(len(_SEG_COLS) + 3))
    for k in changed:
        if items[k][1] is None:
            continue
        row, profile, segs = items[k][1]
        store._conn.execute(
            f"INSERT INTO edls (job_id, ep_key, {cols}, confidence_profile, "
            f"profile_digits) VALUES ({ph})",
            (job_id, k, *row, *_profile_blob(profile)))
        store._conn.executemany(
            f"INSERT INTO edl_segments (job_id, ep_key, pos, {seg_cols}) "
            f"VALUES ({seg_ph})", [(job_id, k, *s) for s in segs])
    if cached is None:
        # a EDL que ainda estava na coluna provisória de job_episodes veio
        # para o dict ao carregar: agora o dict manda (gravada aqui, ou
        # descartada — rematch) e a cópia velha sai, senão a leitura a acha
        old = [r[0] for r in store._conn.execute(
            "SELECT ep_key FROM job_episodes WHERE job_id = ? "
            "AND edl IS NOT NULL", (job_id,)) if r[0] not in missed]
        store._conn.executemany(
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
        for r in store._conn.execute(
                f"SELECT job_id, ep_key, pos, {seg_cols} FROM edl_segments "
                f"WHERE job_id IN ({ph}) ORDER BY job_id, ep_key, pos", chunk):
            segs.setdefault((r[0], r[1]), []).append(r[2:])
        for r in store._conn.execute(
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
