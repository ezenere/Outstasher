"""Candidatos de busca de um job (filmes e séries), no formato normalizado.

Antes cada lista vivia copiada no documento do job: `search` (viáveis), uma
SEGUNDA cópia em `fallbacks` (reservas — todas também em `search`), uma
terceira em `current`/`video_torrent`/`audio_torrent` (o escolhido), o rastro
inteiro de cada consulta em `events.data`, e nas séries `search_tv` repetia o
MESMO pack em cada episódio (no servidor: 2.185 gravados, 124 únicos). Agora
cada candidato existe UMA vez e o resto é atributo ou referência:

    job["search_queries"] = [{"n", "role", "query", "ts", "results"}]
    job["search_candidates"] = [{
        "id",          # estável: é o que a UI devolve na escolha/troca
        "role",        # audio/video (filme) ou original/dubbed (série)
        "query",       # n da consulta cujo rastro o mostra (None: sem rastro)
        "title", "tracker", "seeders", "size", "edition", "quality",
        "coverage",    # rótulo de cobertura (série: "S01 completa"...)
        "score", "tier", "year_match", "rejected", "chosen",
        "magnet", "link", "infohash", "files",
        "trace",       # posição no rastro da consulta (o que a lupa mostra)
        "rank",        # posição na lista escolhível do papel (None: só rastro)
        "fallback",    # posição na fila de reservas do watchdog (filme)
        "active",      # é o torrent do papel no qBittorrent agora (filme)
        "episodes",    # série: {ep_key: posição no ranking daquele episódio}
    }]

Espelha as tabelas search_queries / search_candidates / candidate_episodes
(services/store/candidates.py decompõe e recompõe). As VISÕES que a UI conhece
(`search`, `video_torrent`, `current`, o rastro dos eventos...) são montadas
na leitura pelas funções daqui.

Módulo folha: não importa nada do projeto.
"""
import hashlib
import re
from datetime import datetime

ROLE_LETTER = {"audio": "a", "video": "v", "original": "o", "dubbed": "d"}
_LETTER_ROLE = {v: k for k, v in ROLE_LETTER.items()}

# campos de um candidato, na ordem das colunas (os de posição/estado no fim)
FIELDS = ("id", "role", "query", "title", "tracker", "seeders", "size",
          "edition", "quality", "coverage", "score", "tier", "year_match",
          "rejected", "chosen", "magnet", "link", "infohash", "files",
          "trace", "rank", "fallback", "active", "episodes")
# campos que vêm do indexador/selector (o resto é posição/estado no job)
DATA_FIELDS = ("title", "tracker", "seeders", "size", "edition", "quality",
               "coverage", "score", "tier", "year_match", "rejected", "chosen",
               "magnet", "link", "infohash", "files")


def new(role: str, cid: str, src: dict | None = None, **kw) -> dict:
    """Candidato canônico (todas as chaves presentes): copia de `src` só os
    campos de dado; posição/estado vêm por `kw`."""
    row = {k: None for k in FIELDS}
    row.update(active=False, episodes={})
    for k in DATA_FIELDS:
        if src and k in src:
            row[k] = src[k]
    row.update(kw)
    row["id"], row["role"] = cid, role
    return row


def rows(job: dict) -> list[dict]:
    return job.setdefault("search_candidates", [])


def queries(job: dict) -> list[dict]:
    return job.setdefault("search_queries", [])


def reset(job: dict):
    """Busca nova: os candidatos e consultas anteriores não valem mais."""
    job["search_candidates"] = []
    job["search_queries"] = []


def add_query(job: dict, role: str, query: str, results: int | None) -> int:
    """Registra uma consulta avaliada (o evento "candidates" aponta para ela)."""
    qs = queries(job)
    n = max((q["n"] for q in qs), default=-1) + 1
    qs.append({"n": n, "role": role, "query": query,
               "ts": datetime.now().isoformat(timespec="seconds"),
               "results": results})
    return n


# -------------------- consultas --------------------

def of_role(job: dict, role: str) -> list[dict]:
    return [c for c in job.get("search_candidates") or [] if c["role"] == role]


def selectable(job: dict, role: str) -> list[dict]:
    """Lista escolhível do papel, na ordem do rank (a antiga job["search"][role])."""
    return sorted((c for c in of_role(job, role) if c["rank"] is not None),
                  key=lambda c: c["rank"])


def for_episode(job: dict, role: str, key: str) -> list[dict]:
    """Ranking de UM episódio (a antiga job["search_tv"][role][key])."""
    return sorted((c for c in of_role(job, role) if key in (c["episodes"] or {})),
                  key=lambda c: c["episodes"][key])


def fallbacks(job: dict, role: str) -> list[dict]:
    """Fila de reservas do watchdog (a antiga job["fallbacks"][role])."""
    return sorted((c for c in of_role(job, role) if c["fallback"] is not None),
                  key=lambda c: c["fallback"])


def active(job: dict, role: str) -> dict | None:
    """Torrent do papel no qBittorrent agora (o antigo job["current"][role])."""
    return next((c for c in of_role(job, role) if c["active"]), None)


# id de candidato de série no formato antigo ("S01E02:o3" = 4º do ranking do
# S01E02 no papel original): a UI aberta antes da normalização ainda pode
# mandar um desses, e a posição no episódio o resolve sem ambiguidade
_LEGACY_TV_ID = re.compile(r"^(S\d+E\d+):([od])(\d+)$")


def find(job: dict, cid: str | None, role: str | None = None,
         selectable_only: bool = False) -> dict | None:
    if cid is None:
        return None
    pool = (of_role(job, role) if role else list(job.get("search_candidates") or []))
    if selectable_only:
        pool = [c for c in pool if c["rank"] is not None]
    hit = next((c for c in pool if c["id"] == cid), None)
    if hit is not None:
        return hit
    m = _LEGACY_TV_ID.match(str(cid))
    if m and (role is None or _LETTER_ROLE[m.group(2)] == role):
        key, pos = m.group(1), int(m.group(3))
        r = _LETTER_ROLE[m.group(2)]
        return next((c for c in pool if c["role"] == r
                     and (c["episodes"] or {}).get(key) == pos), None)
    return None


# -------------------- mutações (filme) --------------------

def ensure(job: dict, role: str, cand: dict) -> dict:
    """O candidato do job com este id; se não existe (quem chamou montou o
    dict à mão), entra no job sem posição na lista escolhível."""
    hit = find(job, cand.get("id"), role)
    if hit is not None:
        return hit
    row = new(role, cand.get("id") or f"x:{len(rows(job))}", cand)
    rows(job).append(row)
    return row


def custom_id(url: str) -> str:
    """Id do candidato de um magnet/link do usuário: o MESMO link dá o mesmo
    id em qualquer processo. Era hash() do Python, que muda a cada execução
    (PYTHONHASHSEED): com os candidatos em tabela, o mesmo link colado de novo
    depois de um restart virava outra linha em vez de achar a que já existe."""
    return "custom:" + hashlib.sha1(url.strip().encode("utf-8")).hexdigest()[:12]


def add_custom(job: dict, role: str, cand: dict) -> dict:
    """Magnet/link do usuário: vira candidato do papel e vai para o TOPO da
    lista escolhível (aparece na lista e pode voltar como reserva depois)."""
    row = find(job, cand["id"], role)
    if row is None:
        row = new(role, cand["id"], cand)
        rows(job).append(row)
    else:  # mesmo link de novo: atualiza o título informado, não duplica
        row.update({k: cand[k] for k in DATA_FIELDS if k in cand})
    ordered = [row] + [c for c in selectable(job, role) if c is not row]
    for i, c in enumerate(ordered):
        c["rank"] = i
    return row


def set_active(job: dict, role: str, cand: dict | None):
    cid = cand["id"] if cand else None
    for c in of_role(job, role):
        c["active"] = c["id"] == cid


def set_fallbacks(job: dict, role: str, ordered: list[dict]):
    pos = {c["id"]: i for i, c in enumerate(ordered)}
    for c in of_role(job, role):
        c["fallback"] = pos.get(c["id"])


# -------------------- séries --------------------

def set_series(job: dict, role: str, per_episode: dict[str, list[dict]]):
    """Ranking por episódio -> candidatos únicos + posição em cada episódio.

    O mesmo pack aparece no ranking de cada episódio que cobre; vira UM
    candidato com a posição dele em cada um. Só colapsa cópias IDÊNTICAS
    (mesmos dados) — o mesmo torrent vindo de outro tracker continua sendo
    outro candidato, como no ranking. O id que o candidato já traga (testes,
    normalização do formato antigo) é mantido; sem id, ganha "<papel><n>"."""
    kept = [c for c in rows(job) if c["role"] != role]
    by_data: dict[tuple, dict] = {}
    novos: list[dict] = []
    used = {c["id"] for c in kept}
    for key, cands in per_episode.items():
        for pos, c in enumerate(cands):
            sig = tuple(repr(c.get(k)) for k in DATA_FIELDS)
            row = by_data.get(sig)
            if row is None:
                cid = c.get("id")
                if not cid or cid in used:
                    cid = f"{ROLE_LETTER[role]}{len(novos)}"
                    while cid in used:
                        cid += "'"
                used.add(cid)
                row = new(role, cid, c, rank=len(novos))
                by_data[sig] = row
                novos.append(row)
            row["episodes"].setdefault(key, pos)
    job["search_candidates"] = kept + novos


# -------------------- visões (o JSON que a UI conhece) --------------------

def movie_view(c: dict) -> dict:
    """Candidato da lista de escolha/troca de filme (Candidate na UI)."""
    return {"id": c["id"], "title": c["title"], "tracker": c["tracker"],
            "seeders": c["seeders"], "size": c["size"], "edition": c["edition"],
            "quality": c["quality"], "score": c["score"],
            "magnet": c["magnet"], "link": c["link"]}


def series_view(c: dict) -> dict:
    """Candidato de série (SeriesCandidate na UI) — o antigo slim do search_tv."""
    return {"id": c["id"], "title": c["title"], "tracker": c["tracker"],
            "seeders": c["seeders"], "size": c["size"], "quality": c["quality"],
            "coverage": c["coverage"], "score": c["score"], "tier": c["tier"],
            "magnet": c["magnet"], "link": c["link"],
            "infohash": c["infohash"], "files": c["files"]}


def trace_view(c: dict) -> dict:
    """Linha do rastro de uma consulta, no shape do trace do selector (sem id:
    a tabela da lupa casa o torrent em uso por título/tracker, como antes)."""
    out = {"title": c["title"], "tracker": c["tracker"], "seeders": c["seeders"],
           "size": c["size"], "edition": c["edition"], "quality": c["quality"],
           "score": c["score"], "rejected": c["rejected"],
           "chosen": bool(c["chosen"]), "year_match": bool(c["year_match"])}
    if c["coverage"] is not None:
        out["coverage"] = c["coverage"]
    return out


def torrent_view(c: dict | None) -> dict | None:
    """O escolhido de um papel no card/detalhe (antigo video_torrent/audio_torrent)."""
    if c is None:
        return None
    return {"title": c["title"], "seeders": c["seeders"], "size": c["size"],
            "score": c["score"], "edition": c["edition"], "id": c["id"],
            "tracker": c["tracker"]}


def search_view(job: dict) -> dict | None:
    """A antiga job["search"] de filme: {audio: [...], video: [...]}, ou None
    quando o job não tem lista para escolher (busca não terminou/falhou,
    recompressão, arquivos locais, série). "Pular busca" abre a escolha com
    as listas vazias — é nela que o magnet é colado."""
    if job.get("media_type") == "tv":
        return None
    lists = {r: [movie_view(c) for c in selectable(job, r)] for r in ("audio", "video")}
    if lists["audio"] or lists["video"] or job.get("skip_search"):
        return lists
    return None


def movie_fields(job: dict) -> dict:
    """Campos derivados de filme que a UI lê no job: o escolhido de cada papel
    e o candidato ativo (para avisar que a nova escolha descarta o atual)."""
    v, a = active(job, "video"), active(job, "audio")
    return {"video_torrent": torrent_view(v), "audio_torrent": torrent_view(a),
            "current": ({"video": movie_view(v) if v else None,
                         "audio": movie_view(a) if a else None}
                        if v or a else None)}


def query_trace(job: dict, n: int) -> list[dict] | None:
    """Rastro de uma consulta (o que o evento "candidates" mostrava inline)."""
    if not any(q["n"] == n for q in job.get("search_queries") or []):
        return None
    hits = sorted((c for c in job.get("search_candidates") or []
                   if c["query"] == n and c["trace"] is not None),
                  key=lambda c: c["trace"])
    return [trace_view(c) for c in hits]


def resolve_events(job: dict, events: list[dict]) -> list[dict]:
    """Evento "candidates" guarda só a referência à consulta: aqui ele volta a
    ter a lista que a lupa renderiza. Evento antigo (lista inline) passa igual."""
    out = []
    for ev in events:
        d = ev.get("data")
        if (ev.get("kind") == "candidates" and isinstance(d, dict)
                and "candidates" not in d and "search_query" in d):
            trace = query_trace(job, d["search_query"])
            if trace is not None:
                ev = {**ev, "data": {**d, "candidates": trace}}
        out.append(ev)
    return out
