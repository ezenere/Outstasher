"""Pontuacao e escolha do melhor torrent.

Dois modos:
- "video": para a versao original/ingles — prioriza qualidade de imagem.
- "audio": para a versao dublada — prioriza qualidade de audio e exige
  marcador do idioma no nome (ou vir da busca pelo titulo traduzido).

pick_best devolve tambem o "trace" da decisao: todos os candidatos avaliados,
com score e motivo de rejeicao, para exibir no frontend.
"""
import html
import math
import re
import unicodedata

import config

RESOLUTION_SCORES = {
    "2160p": 40, "4k": 40, "uhd": 40,
    "1080p": 30,
    "720p": 18,
    "480p": 6, "sd": 4,
}

SOURCE_SCORES = {
    "remux": 25,
    "blu-ray": 18, "bluray": 18, "bdrip": 14, "brrip": 12,
    "web-dl": 14, "webdl": 14, "web dl": 14,
    "webrip": 10, "web": 8,
    "hdtv": 5,
    "dvdrip": 4,
    "hdcam": -60, "camrip": -60, "cam": -60, "telesync": -60, "hdts": -60,
    " ts ": -60, ".ts.": -60, "tc": -30,
}

AUDIO_SCORES = {
    "truehd": 30, "true-hd": 30, "atmos": 30,
    "dts-hd": 28, "dtshd": 28, "dts-x": 28,
    "dts": 20,
    "eac3": 16, "e-ac3": 16, "ddp": 16, "dd+": 16, "dolby digital plus": 16,
    "ac3": 12, "dd5.1": 12, "dd 5.1": 12,
    "flac": 18,
    "5.1": 10, "7.1": 12,
    "aac": 6,
}

MAX_TRACE = 40

# Sentinela: sem restricao de edicao (None significa "corte normal/theatrical")
ANY_EDITION = "__any__"

# Apenas edicoes que MUDAM O CORTE do filme (duracao/conteudo) — remaster/IMAX
# nao alteram a linha do tempo e nao atrapalham o alinhamento dos audios.
# Comparados contra o titulo SEM acento (_fold), entao os padroes tambem sao
# sem acento: "versao" pega "Versão", "estendida" pega "Estendida".
EDITION_PATTERNS = [
    (re.compile(r"extended|estendid[oa]|extendid[oa]", re.I), "extended"),
    (re.compile(r"director'?s[ ._-]?cut|versao[ ._-]?do[ ._-]?diretor", re.I),
     "director's cut"),
    (re.compile(r"final[ ._-]?cut|corte[ ._-]?final", re.I), "final cut"),
    (re.compile(r"ultimate[ ._-]?(cut|edition)", re.I), "ultimate"),
    (re.compile(r"special[ ._-]?edition|edicao[ ._-]?especial", re.I), "special edition"),
    (re.compile(r"\bunrated\b", re.I), "unrated"),
    (re.compile(r"\buncut\b", re.I), "uncut"),
    (re.compile(r"\btheatrical\b|\bcinema\b", re.I), None),  # explicito = corte normal
]


def edition_of(title: str) -> str | None:
    """Edicao do corte no nome do torrent; None = corte normal (theatrical)."""
    folded = _fold(title)  # sem acento/entidades, para pegar "Versão", "Estendido"
    for pattern, tag in EDITION_PATTERNS:
        if pattern.search(folded):
            return tag
    return None


def _keyword_score(title_lower: str, table: dict[str, int]) -> int:
    best = 0
    worst = 0
    for kw, score in table.items():
        if kw in title_lower:
            if score > best:
                best = score
            if score < worst:
                worst = score
    return best + worst  # penalidades (cam etc.) sempre contam


def _seeders_score(seeders: int) -> float:
    return min(10.0, 3 * math.log10(seeders + 1) * 3)


def _clean(text: str) -> str:
    """Entidades HTML decodificadas + minúsculas (Jackett devolve 'T&oacute;quio').

    MANTÉM acentos — eles diferenciam marcador forte ('dual áudio' brasileiro)
    de fraco ('dual audio' pode ser Hindi+English).
    """
    return html.unescape(text).lower()


def _fold(text: str) -> str:
    """_clean + sem acentos, para comparar títulos ('Tóquio' == 'Toquio')."""
    nfkd = unicodedata.normalize("NFKD", _clean(text))
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def _matches_movie(title: str, movie_title: str, year: str) -> bool:
    """Confere se o resultado parece ser do filme certo (todas as palavras do titulo)."""
    folded = _fold(title)
    words = [w for w in re.split(r"\W+", _fold(movie_title)) if len(w) > 1]
    return not words or all(w in folded for w in words)


def matches_title(title: str, movie_title: str) -> bool:
    """True se o nome do torrent contém o título (sem acentos/entidades HTML)."""
    return _matches_movie(title, movie_title, "")


# Bônus (modo audio) para marcador forte: dublagem confirmada no idioma vence
# "dual"/"multi" genérico mesmo com release de qualidade um pouco menor.
STRONG_MARKER_BONUS = 25


def marker_strength(title: str, language: str) -> int:
    """2 = marcador forte (dublagem confirmada), 1 = fraco ('dual'...), 0 = nenhum."""
    t = " " + _clean(title) + " "
    info = config.LANGUAGES[language]
    if any(m in t for m in info["markers_strong"]):
        return 2
    if any(m in t for m in info["markers_weak"]):
        return 1
    return 0


def has_language_marker(title: str, language: str) -> bool:
    return marker_strength(title, language) > 0


def score(result: dict, mode: str, language: str | None = None) -> float:
    t = " " + _clean(result["title"]) + " "
    s = _seeders_score(result["seeders"])
    s += _keyword_score(t, SOURCE_SCORES)
    if mode == "video":
        s += _keyword_score(t, RESOLUTION_SCORES) * 2
        s += _keyword_score(t, AUDIO_SCORES) * 0.3
    else:  # audio
        s += _keyword_score(t, AUDIO_SCORES) * 2
        s += _keyword_score(t, RESOLUTION_SCORES) * 0.5
        if language and marker_strength(result["title"], language) == 2:
            s += STRONG_MARKER_BONUS
    return s


def rank(results: list[dict], mode: str, movie_title: str, year: str,
         language: str | None = None,
         require_language: bool = False,
         required_edition: str | None = ANY_EDITION) -> tuple[list[dict], list[dict]]:
    """Retorna (viáveis ordenados por score desc, trace de todos os avaliados).

    Cada viável é o resultado completo (magnet/link) + score + edition.
    required_edition: ANY_EDITION libera qualquer corte; None exige corte normal;
    uma string ("extended", ...) exige aquele corte — para as duas versões
    baixadas serem do MESMO corte e os áudios alinharem.
    """
    pairs: list[tuple[dict, dict]] = []
    for r in results:
        edition = edition_of(r["title"])
        cand = {
            "title": r["title"],
            "tracker": r.get("tracker"),
            "seeders": r["seeders"],
            "size": r["size"],
            "edition": edition,
            "score": None,
            "rejected": None,
            "chosen": False,
        }
        if not (r.get("magnet") or r.get("link")):
            cand["rejected"] = "sem magnet/link"
        elif not _matches_movie(r["title"], movie_title, year):
            cand["rejected"] = "título não bate"
        elif require_language and language and not has_language_marker(r["title"], language):
            cand["rejected"] = f"sem marcador de idioma ({config.LANGUAGES[language]['label']})"
        elif required_edition != ANY_EDITION and edition != required_edition:
            cand["rejected"] = (f"corte diferente da outra versão "
                                f"({edition or 'normal'} ≠ {required_edition or 'normal'})")
        elif r["seeders"] <= 0:
            cand["rejected"] = "sem seeders"
        else:
            s = score(r, mode, language)
            cand["score"] = round(s, 1)
            if s <= -30:
                cand["rejected"] = "qualidade muito baixa (CAM/TS)"
        pairs.append((cand, r))

    viable = [(c, r) for c, r in pairs if c["rejected"] is None]
    viable.sort(key=lambda p: p[0]["score"], reverse=True)
    ranked = []
    for c, r in viable:
        item = dict(r)
        item["score"] = c["score"]
        item["edition"] = c["edition"]
        ranked.append(item)

    trace = sorted(
        (c for c, _ in pairs),
        key=lambda c: (not c["chosen"], c["rejected"] is not None,
                       -(c["score"] if c["score"] is not None else -1e9)))
    return ranked, trace[:MAX_TRACE]


def pick_best(results: list[dict], mode: str, movie_title: str, year: str,
              language: str | None = None,
              require_language: bool = False,
              required_edition: str | None = ANY_EDITION) -> tuple[dict | None, list[dict]]:
    """Retorna (melhor resultado ou None, trace) — atalho em cima de rank()."""
    ranked, trace = rank(results, mode, movie_title, year,
                         language=language, require_language=require_language,
                         required_edition=required_edition)
    if not ranked:
        return None, trace
    best = ranked[0]
    for c in trace:
        if c["title"] == best["title"] and c["score"] == best["score"]:
            c["chosen"] = True
            break
    return best, trace
