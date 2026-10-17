"""Escalares do job e metadados do TMDB: os codecs.

As colunas da `jobs`, as tabelas filhas pequenas e as compartilhadas
(media*), o codec de cada chave (_CODECS: _enc_* / _dec_*), a máscara
`keys`, _encode / _decode e a normalização do formato antigo
(_legacy_scalars). Só funções puras; o SQL dessas linhas está em
scalar_rows.
"""
from operator import itemgetter

from .common import _digest
from .series import _PROGRESS_KEYS


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
