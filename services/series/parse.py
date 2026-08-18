"""Parsing de nomes de release de TV: episódios, packs e sinais de suspeita.

Tudo aqui é função pura sobre o TÍTULO do torrent (mais metadados opcionais do
TMDB/Jackett) — nada de rede ou estado. O selector de séries e o gate de
torrents incompatíveis são construídos em cima destas primitivas.
"""
import re
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlparse

from services import selector

# -------------------- referências de episódio --------------------

# SxxEyy com separadores comuns (S01E02, s1e2, S01.E02, S01_E02, S01 E02).
# Range opcional na sequência: S01E01-E03 / S01E01-03 / S01E01~03.
# "T01E02" também: T de Temporada, convenção de packs brasileiros (só na forma
# com o E — "T2" solto seria ambíguo com sequência de filme, e não é aceito
# como temporada solta).
_SXXEYY_RE = re.compile(
    r"[sStT](\d{1,2})[ ._-]?[eE](\d{1,3})"
    r"(?:[ ]?[-~][ ]?[eE]?(\d{1,3}))?")

# forma "1x02" (scene antiga). e com EXATAMENTE 2 dígitos: 3 casaria o 264 de
# "1x264"; resoluções ("1920x1080") não casam porque o lookbehind barra dígito
# antes do bloco do s.
_NXMM_RE = re.compile(r"(?<![\dxX])(\d{1,2})[xX](\d{2})(?!\d)")


def parse_episode_refs(title: str) -> list[tuple[int, int]]:
    """Todos os (temporada, episódio) referenciados no título, em ordem.

    Ranges expandem (S01E01-E03 -> E01..E03). Duplicatas removidas preservando
    a ordem. Lista vazia = título sem referência explícita de episódio.
    """
    out: list[tuple[int, int]] = []
    for m in _SXXEYY_RE.finditer(title):
        season, e1 = int(m.group(1)), int(m.group(2))
        e2 = int(m.group(3)) if m.group(3) else e1
        if e2 < e1:  # "S01E05-02" não é range válido; fica só o primeiro
            e2 = e1
        # teto defensivo: um range absurdo (E01-999) é erro de parse, não pack
        for e in range(e1, min(e2, e1 + 99) + 1):
            out.append((season, e))
    if not out:
        for m in _NXMM_RE.finditer(title):
            out.append((int(m.group(1)), int(m.group(2))))
    seen: set[tuple[int, int]] = set()
    uniq = []
    for ref in out:
        if ref not in seen:
            seen.add(ref)
            uniq.append(ref)
    return uniq


# -------------------- temporadas / packs --------------------

# "S01" solto (sem Exx logo depois — senão é referência de episódio)
_SEASON_SOLO_RE = re.compile(r"(?<![\w])[sS](\d{1,2})(?![\d]|[ ._-]?[eE]\d)")
# "Season 1" / "Temporada 1" / "1ª temporada" / "3a temporada"
# "1ª temporada": entre o número e a palavra pode vir o ordinal (ª/a/°) OU o
# LIXO que o Jackett gera ao corromper o "ª" (U+FFFD repetido, "Âª"...) — por
# isso [^\d\s]{0,6}: um bloco curto de qualquer coisa que não seja dígito nem
# espaço (o "ª" é \w em regex Unicode, então [^\w\s] não serviria). Sem isso
# "1���� Temporada Completa" caía em "Completa" = série inteira.
_SEASON_WORD_RE = re.compile(
    r"\bseason[ ._-]?(\d{1,2})\b|\btemporada[ ._-]?(\d{1,2})\b"
    r"|\b(\d{1,2})[^\d\s]{0,6}[ ._-]?temporada\b", re.I)
# ranges: "S01-S05", "S01 a S03", "Seasons 1-5", "Temporadas 1 a 5"
_SEASON_RANGE_RE = re.compile(
    r"(?<![\w])[sS](\d{1,2})[ ._-]?(?:-|a|~|ate|até|to)[ ._-]?[sS]?(\d{1,2})(?![\w])"
    r"|\b(?:seasons|temporadas)[ ._-]?(\d{1,2})[ ._-]?(?:-|a|~|ate|até|to)[ ._-]?(\d{1,2})\b",
    re.I)
# série completa / batch (anime) / coleção
_COMPLETE_RE = re.compile(
    r"\bcomplete[ ._-]?(?:series|collection|pack)?\b|\bcompleta\b|\bintegral\b"
    r"|\bbatch\b|\bcole[cç][aã]o\b|\btodas[ ._-]as[ ._-]temporadas\b", re.I)


@dataclass
class Coverage:
    """O que um torrent cobre, deduzido do nome.

    kind:
    - "episode": um episódio só
    - "multi_episode": mais de um episódio explícito (range/arquivo duplo)
    - "season_pack": temporada(s) inteira(s) — episódios dependem do TMDB
    - "complete": série completa (temporadas não enumeradas no nome)
    - "unknown": nome não diz nada — só serve com inspeção manual
    """
    kind: str
    seasons: set[int] = field(default_factory=set)
    # episódios explícitos; None quando o nome só fala em temporada/completa
    episodes: list[tuple[int, int]] | None = None

    def covers(self, season: int, episode: int,
               known_seasons: set[int] | None = None) -> bool:
        """True se este torrent deve conter o episódio pedido.

        known_seasons: temporadas existentes na série (TMDB) — usado por
        "complete", que não enumera temporadas no nome.
        """
        if self.kind in ("episode", "multi_episode"):
            return (season, episode) in (self.episodes or [])
        if self.kind == "season_pack":
            return season in self.seasons
        if self.kind == "complete":
            return known_seasons is None or season in known_seasons
        return False

    def label(self) -> str:
        """Rótulo curto para o trace/UI ("S01E02", "S01 completa", ...)."""
        if self.kind == "episode" and self.episodes:
            s, e = self.episodes[0]
            return f"S{s:02d}E{e:02d}"
        if self.kind == "multi_episode" and self.episodes:
            first, last = self.episodes[0], self.episodes[-1]
            return (f"S{first[0]:02d}E{first[1]:02d}"
                    f"-S{last[0]:02d}E{last[1]:02d}")
        if self.kind == "season_pack":
            ss = sorted(self.seasons)
            if len(ss) == 1:
                return f"S{ss[0]:02d} completa"
            return "Temporadas " + ", ".join(f"S{s:02d}" for s in ss)
        if self.kind == "complete":
            return "Série completa"
        return "?"


def parse_coverage(title: str) -> Coverage:
    """Classifica o que o torrent cobre a partir do nome."""
    eps = parse_episode_refs(title)
    if eps:
        kind = "episode" if len(eps) == 1 else "multi_episode"
        return Coverage(kind, {s for s, _ in eps}, eps)

    seasons: set[int] = set()
    for m in _SEASON_RANGE_RE.finditer(title):
        a, b = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
        lo, hi = int(a), int(b)
        if lo <= hi and hi - lo < 60:  # range absurdo = erro de parse
            seasons.update(range(lo, hi + 1))
    if not seasons:
        for m in _SEASON_WORD_RE.finditer(title):
            n = next(g for g in m.groups() if g)
            seasons.add(int(n))
        for m in _SEASON_SOLO_RE.finditer(title):
            seasons.add(int(m.group(1)))
    if seasons:
        return Coverage("season_pack", seasons)
    if _COMPLETE_RE.search(title):
        return Coverage("complete")
    return Coverage("unknown")


def strip_episode_tokens(title: str) -> str:
    """Remove tokens de episódio/temporada do nome, sobrando o título da série
    (+ tags de qualidade) — para casar contra o título do TMDB com o matching
    de filmes (todas as palavras)."""
    t = _SXXEYY_RE.sub(" ", title)
    t = _NXMM_RE.sub(" ", t)
    t = _SEASON_RANGE_RE.sub(" ", t)
    t = _SEASON_WORD_RE.sub(" ", t)
    t = _SEASON_SOLO_RE.sub(" ", t)
    t = _COMPLETE_RE.sub(" ", t)
    return re.sub(r"\s{2,}", " ", t).strip(" .-_")


# -------------------- resolução segura para packs --------------------

def pack_resolution_tier(title: str) -> tuple[int, str]:
    """(tier, rótulo) tolerante a packs multi-resolução.

    selector.quality_tier usa resolution_of, que devolve None quando o nome
    anuncia MAIS de uma resolução — certo para um arquivo único (não dá para
    saber qual é), mas um pack "1080p + 720p" contém a melhor: classificamos
    pela MELHOR resolução anunciada, reaproveitando o resto da escada.
    """
    folded = selector._fold(title)
    found = {label for pat, label in selector.RESOLUTION_PATTERNS
             if pat.search(folded)}
    if len(found) <= 1:
        return selector.quality_tier(title)
    best = max(found, key=lambda r: selector._RES_RANK[r])
    # reclassifica com o nome reduzido à melhor resolução: troca as demais por
    # espaço para o quality_tier enxergar uma só
    cleaned = folded
    for pat, label in selector.RESOLUTION_PATTERNS:
        if label != best:
            cleaned = pat.sub(" ", cleaned)
    tier, label = selector.quality_tier(cleaned)
    return tier, label + " (pack misto)"


# -------------------- identidade estável de torrent --------------------

_BTIH_RE = re.compile(r"btih:([0-9a-fA-F]{40}|[A-Za-z2-7]{32})")


def torrent_identity(result: dict) -> str:
    """Chave estável de um torrent para dedup/set-cover (mesma semântica da
    identidade usada no pipeline de filmes, replicada aqui para não importar a
    orquestração): infohash > btih do magnet > param file= do link > link sem
    query > título."""
    if result.get("infohash"):
        return f"hash:{str(result['infohash']).lower()}"
    magnet = result.get("magnet") or ""
    m = _BTIH_RE.search(magnet)
    if m:
        return f"hash:{m.group(1).lower()}"
    link = result.get("link") or ""
    if link:
        q = parse_qs(urlparse(link).query)
        if q.get("file"):
            return f"file:{q['file'][0]}"
        return f"link:{link.split('?', 1)[0]}"
    return f"title:{result.get('title', '')}"


# -------------------- sinais de incompatibilidade --------------------

# numeração absoluta de anime: " - 015" / " - 1032" sem SxxEyy no nome
_ABSOLUTE_RE = re.compile(r"[ ._]-[ ._](\d{2,4})(?!\d)(?![pi])")
# episódio nomeado por data (programa diário: talk show, telejornal)
_DATED_RE = re.compile(r"\b(19|20)\d{2}[ ._-]\d{2}[ ._-]\d{2}\b")
# ordem de episódios explícita no nome (release já avisa que difere da exibição)
_ORDER_MARKER_RE = re.compile(
    r"\b(?:dvd|bd|blu-?ray|broadcast|tv|aired?|production)[ ._-]?order\b", re.I)


def suspicious_signals(title: str, files_count: int | None = None,
                       tmdb_episode_count: int | None = None,
                       alt_order_groups: list[dict] | None = None) -> list[str]:
    """Sinais de que o torrent pode ser INCOMPATÍVEL com a ordem/divisão de
    episódios do TMDB. Só detecção — a decisão é sempre do usuário (gate).

    files_count: nº de arquivos do torrent (Jackett, quando disponível).
    tmdb_episode_count: nº de episódios da(s) temporada(s) cobertas no TMDB.
    alt_order_groups: episode_groups do TMDB (ordens alternativas conhecidas).
    """
    signals: list[str] = []
    cov = parse_coverage(title)

    if not parse_episode_refs(title) and _ABSOLUTE_RE.search(title) \
            and cov.kind in ("unknown", "complete"):
        n = _ABSOLUTE_RE.search(title).group(1)
        signals.append(
            f"numeração absoluta ('- {n}') sem SxxEyy — a ordem pode não "
            f"corresponder às temporadas do TMDB")

    if _DATED_RE.search(title):
        signals.append("episódio nomeado por data — numeração de temporada "
                       "provavelmente não se aplica")

    m = _ORDER_MARKER_RE.search(title)
    if m:
        signals.append(f"ordem de episódios explícita no nome ('{m.group(0)}') "
                       f"— pode diferir da ordem de exibição")

    if (cov.kind in ("season_pack", "complete") and files_count
            and tmdb_episode_count):
        # menos arquivos que episódios = episódios fundidos ou faltando; MUITO
        # mais arquivos tende a ser extras/samples (não é sinal por si só)
        if files_count < tmdb_episode_count:
            signals.append(
                f"pack com {files_count} arquivo(s) para "
                f"{tmdb_episode_count} episódios no TMDB — episódios podem "
                f"estar fundidos ou faltando")

    if alt_order_groups:
        names = ", ".join(g.get("name") or "?" for g in alt_order_groups[:3])
        signals.append(
            f"a série tem ordem(ns) alternativa(s) de episódios no TMDB "
            f"({names}) — versões de mídias diferentes podem numerar "
            f"episódios de outro jeito")

    return signals
