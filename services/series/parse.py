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
# Temporada com zero a mais ("S012E05", visto em packs brasileiros) é a S12.
_SXXEYY_RE = re.compile(
    r"[sStT]0?(\d{1,2})[ ._-]?[eE](\d{1,3})"
    r"(?:[ ]?[-~][ ]?[eE]?(\d{1,3}))?")

# forma "1x02" (scene antiga). e com EXATAMENTE 2 dígitos: 3 casaria o 264 de
# "1x264"; resoluções ("1920x1080") não casam porque o lookbehind barra dígito
# antes do bloco do s.
_NXMM_RE = re.compile(r"(?<![\dxX])(\d{1,2})[xX](\d{2})(?!\d)")


def parse_episode_refs(title: str) -> list[tuple[int, int]]:
    """Todos os (temporada, episódio) referenciados no título, em ordem.

    Ranges expandem (S01E01-E03 -> E01..E03). Duplicatas removidas preservando
    a ordem. Lista vazia = título sem referência explícita de episódio.

    Ordem de confiança: SxxEyy, depois "1x02", e só então o episódio POR
    EXTENSO ao lado da temporada ("9ª Temporada 08º Episódio") — um nome que
    já traz a forma compacta não precisa da por extenso.
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
    if not out:
        out = _word_episode_refs(title)[0]
    seen: set[tuple[int, int]] = set()
    uniq = []
    for ref in out:
        if ref not in seen:
            seen.add(ref)
            uniq.append(ref)
    return uniq


# Pack de UMA temporada que numera os arquivos só pela posição, sem SxxEyy:
# "1 - Nome do Episódio.mp4", "07. Nome.mkv", "Episódio 3.mkv". O número
# precisa vir no INÍCIO do nome e seguido de separador — "1080p..." não casa
# (o lookahead barra o dígito seguinte).
_LEADING_NUM_RE = re.compile(
    r"^\s*(?:ep(?:is[oó]dio)?[ ._-]*)?(\d{1,3})(?!\d)(?=[\s._\-–)\]]|$)", re.I)


def position_refs(names: list[str], season: int,
                  video_ext: set[str] | frozenset[str]) -> dict[str, tuple[int, int]]:
    """Episódio de cada arquivo de um pack de temporada ÚNICA numerado por
    posição: {nome: (season, n)}. Vazio quando a regra não se aplica sem
    ambiguidade — algum arquivo já traz SxxEyy/1x02 (o pack não é desse
    tipo), algum VÍDEO sem número no início, ou dois vídeos com o mesmo
    número. Arquivos que não são vídeo (legendas) entram se tiverem o número.

    `names` podem ser caminhos ("pasta/1 - Nome.mp4"): vale o último trecho."""
    def base(n: str) -> str:
        return n.replace("\\", "/").rsplit("/", 1)[-1]

    def ext(n: str) -> str:
        b = base(n)
        return b[b.rfind("."):].lower() if "." in b else ""

    if any(parse_episode_refs(base(n)) for n in names):
        return {}
    out: dict[str, tuple[int, int]] = {}
    vid_nums: list[int] = []
    for n in names:
        b = base(n)
        stem = b[:b.rfind(".")] if "." in b else b
        m = _LEADING_NUM_RE.match(stem)
        is_video = ext(n) in video_ext
        if not m:
            if is_video:
                return {}
            continue
        num = int(m.group(1))
        if num == 0:
            if is_video:
                return {}
            continue
        if is_video:
            vid_nums.append(num)
        out[n] = (season, num)
    if not vid_nums or len(set(vid_nums)) != len(vid_nums):
        return {}
    return out


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


# -------------------- episódio por extenso --------------------

# Indexadores brasileiros (e alguns gringos) não escrevem SxxEyy: o episódio
# vem por extenso ao lado da temporada — "9ª Temporada 08º Episódio",
# "Temporada 2 Ep. 05", "Season 3 Episode 7", "Temporada 1 E04". Sem ler isso
# o nome só mostrava a temporada e virava pack: caso real, três avulsos
# "9ª Temporada 08º/09º/10º EPISÓDIO" apareceram como "S09 completa" na
# escolha manual e cada um assumiu os três episódios pedidos.
#
# A palavra: episódio(s) com a vogal acentuada livre (o Jackett corrompe o "Ó"
# em U+FFFD ou "Ã“", como faz com o "ª" da temporada), episode(s),
# capítulo(s) e ep/eps — este último sem letra colada, para "Epílogo" não ser
# episódio. O "ep" de "Deep" não casa porque toda forma exige que nada
# alfanumérico venha antes da palavra.
_EP_WORD = (r"(?:epis[^\s\d]{1,4}dios?|episodes?|cap[^\s\d]{1,3}tulos?"
            r"|eps?(?![^\W\d_]))")
# número de episódio DEPOIS da palavra: até 3 dígitos e nada alfanumérico
# colado (fora o "º") — "Ep 720p", "Ep 1080", "Ep 10bit" e o "5.1" do áudio
# (decimal de UM dígito) não viram episódio; "Episode.8.720p" (nome com
# pontos) vira o 8. O "ª" fica de fora de propósito: é ordinal FEMININO, de
# temporada ("Ep 9ª Temporada" não é o episódio 9).
_EP_NUM = r"\d{1,3}(?![^\W_º])(?![.,]\d(?!\d))"
# separador entre números: range ("01-05", "01 a 05", "1 ao 5", "1 to 5") ou
# lista ("01 e 02", "1, 2", "1 & 2"); o 2º número pode repetir o prefixo
# ("Ep01-Ep05", "E01-E05")
_EP_SEP = (r"[ ._]*(?:[-~–+&,]|\b(?:a|ao|à|ate|até|e|and|to|thru)\b)[ ._]*"
           r"(?:ep\.?[ ._]*|e(?=\d))?")
_EP_RANGE_SEP_RE = re.compile(r"[-~–]|\b(?:a|ao|à|ate|até|to|thru)\b", re.I)
# palavra ANTES do número: "Episódio 08", "Ep. 8", "EP08", "Episode 8",
# "Episódio Nº 8", "Episódios 01 a 05", "Ep 01-05" — e o "E08" solto, que só
# vale porque toda forma por extenso exige uma temporada no nome
_EP_AFTER_RE = re.compile(
    rf"(?<![^\W_])(?:{_EP_WORD}\.?[ ._-]*(?:n[º°]\.?[ ._]*|#[ ]*)?|e(?=\d))"
    rf"{_EP_NUM}(?:{_EP_SEP}{_EP_NUM})*", re.I)
# ordinal: º/ª/° (com o ponto europeu "8.º"), "o"/"a" colados ("8o"), e o lixo
# do Jackett no lugar do "º" (U+FFFD repetido, "Âº")
_EP_ORD = r"(?:\.?[ºª°]|[oa](?![^\W_])|\uFFFD{1,4}|Â[º°ª])"
# número ANTES da palavra: "08º Episódio", "8º EP", "8o ep", "01º ao 05º
# Episódios". Nada alfanumérico nem decimal antes do número — senão o "264"
# de "x264" e o "1" de "5.1" virariam episódio. Sem ordinal, um número
# solto só vale com a palavra inteira no singular ("08 Episódio"):
# "10 Episódios" é CONTAGEM (pack de 10), não o episódio 10.
_EP_BEFORE_RE = re.compile(
    rf"(?<![^\W_])(?<!\d[.,])\d{{1,3}}(?!\d)(?P<ord>{_EP_ORD})?"
    rf"(?:{_EP_SEP}\d{{1,3}}(?!\d){_EP_ORD}?)*"
    rf"[ ._-]*(?P<word>{_EP_WORD})(?![^\W\d_])", re.I)
_EP_ORD_LEAD_RE = re.compile(_EP_ORD, re.I)
# UM episódio sem número: "Episódio Final", "Último Episódio", "Episódio
# Piloto", "Season Finale". Com temporada no nome isso não é pack — tratar
# como "S09 completa" faria o plano baixar um arquivo esperando a temporada.
_UNNUMBERED_EP_RE = re.compile(
    r"(?<![^\W_])(?:epis[^\s\d]{1,4}dio|episode|cap[^\s\d]{1,3}tulo)[ ._-]+"
    r"(?:final|piloto|pilot|especial|special)(?![^\W_])"
    r"|(?<![^\W_])(?:[uú]ltimo|final|pilot|piloto)[ ._-]+"
    r"(?:epis[^\s\d]{1,4}dio|episode)(?![^\W_])"
    r"|\b(?:season|series|mid[ ._-]?season)[ ._-]?(?:finale|premiere)\b", re.I)


def _blank(text: str, spans: list[tuple[int, int]]) -> str:
    """Troca os trechos por espaços do MESMO tamanho (as posições continuam
    valendo para as buscas seguintes)."""
    chars = list(text)
    for a, b in spans:
        chars[a:b] = " " * (b - a)
    return "".join(chars)


def _expand_numbers(text: str) -> list[int]:
    """Números de um token de episódio já casado, com ranges expandidos.

    Os números têm de crescer: "01 a 05" expande, "08 e 09" lista; um número
    menor que o anterior ("Ep 08, 2 Áudios") não é episódio e fica de fora."""
    nums = list(re.finditer(r"\d{1,3}", text))
    if not nums:
        return []
    out = [int(nums[0].group())]
    for prev, cur in zip(nums, nums[1:]):
        n = int(cur.group())
        last = out[-1]
        if n <= last:
            continue
        # o que separa os dois números, sem o ordinal colado no anterior
        # ("1º ao 5º", "1a e 2a") — o "a" do ordinal não é o "a" do range
        between = text[prev.end():cur.start()]
        lead = _EP_ORD_LEAD_RE.match(between)
        if lead:
            between = between[lead.end():]
        if _EP_RANGE_SEP_RE.search(between):
            # teto defensivo como no SxxEyy: range absurdo é erro de parse
            out.extend(range(last + 1, min(n, last + 99) + 1))
        else:
            out.append(n)
    return out


def _word_episode_refs(title: str) -> tuple[list[tuple[int, int]],
                                             list[tuple[int, int]]]:
    """(referências, trechos usados) do episódio por extenso + temporada.

    Três passadas, cada uma apagando o que achou para a seguinte não reler:
    1. palavra + número ("Episódio 08", "E08") — apagada antes de procurar a
       temporada, senão "Episódio 08 Temporada 9" leria "08 Temporada" como
       a 8ª temporada;
    2. temporada ("9ª Temporada", "Temporada 9", "Season 9", "S09");
    3. número + palavra ("08º Episódio") — DEPOIS de apagar a temporada, senão
       "Temporada 1 - 10 Episódios" leria "1 - 10" como range de episódios.

    Episódio sem temporada no nome ("Ep. 8", "Episódio 08") não vira
    referência: a numeração pode ser absoluta (anime, novela) e chutar a
    temporada 1 atribuiria o torrent ao episódio errado — fica como hoje, sem
    referência (cobertura "unknown", só atribuível à mão). Temporadas em
    range ("Temporadas 1 a 3 Episódio 5") também não: não dá para saber de
    qual temporada é o episódio.
    """
    episodes: list[tuple[int, list[int]]] = []  # (posição, números)
    spans: list[tuple[int, int]] = []
    for m in _EP_AFTER_RE.finditer(title):
        nums = _expand_numbers(m.group(0))
        if nums:
            episodes.append((m.start(), nums))
            spans.append(m.span())
    masked = _blank(title, spans)

    if _SEASON_RANGE_RE.search(masked):
        return [], []
    seasons: list[tuple[int, int]] = []  # (posição, temporada)
    season_spans: list[tuple[int, int]] = []
    for m in _SEASON_WORD_RE.finditer(masked):
        seasons.append((m.start(), int(next(g for g in m.groups() if g))))
        season_spans.append(m.span())
    for m in _SEASON_SOLO_RE.finditer(masked):
        seasons.append((m.start(), int(m.group(1))))
        season_spans.append(m.span())
    if not seasons:
        return [], []
    masked = _blank(masked, season_spans)

    for m in _EP_BEFORE_RE.finditer(masked):
        nums = _expand_numbers(m.group(0)[:m.start("word") - m.start()])
        word = m.group("word").lower()
        # número solto sem ordinal: só "08 Episódio" (palavra inteira no
        # singular); "10 Episódios"/"10 EPs" é contagem de episódios do pack
        if len(nums) == 1 and not m.group("ord") \
                and (word.endswith("s") or len(word) <= 3):
            continue
        if nums:
            episodes.append((m.start(), nums))
            spans.append(m.span())
    if not episodes:
        return [], []

    distinct = {s for _, s in seasons}
    refs: list[tuple[int, int]] = []
    for pos, nums in sorted(episodes):
        if len(distinct) == 1:
            season = next(iter(distinct))
        else:
            # mais de uma temporada no nome ("Temporada 1 Episódios 1-10 e
            # Temporada 2 Episódios 1-5"): a mais próxima ANTES do episódio,
            # senão a primeira depois (episódio escrito antes da temporada)
            before = [(p, s) for p, s in seasons if p < pos]
            season = (max(before)[1] if before
                      else min((p, s) for p, s in seasons if p > pos)[1])
        refs.extend((season, e) for e in nums)
    return refs, spans


@dataclass
class Coverage:
    """O que um torrent cobre, deduzido do nome.

    kind:
    - "episode": um episódio só
    - "multi_episode": mais de um episódio explícito (range/arquivo duplo)
    - "season_pack": temporada(s) inteira(s) — episódios dependem do TMDB
    - "complete": série completa (temporadas não enumeradas no nome)
    - "unknown": nome não diz nada (ou diz UM episódio sem número, como
      "Episódio Final") — só serve com inspeção manual
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
        # "9ª Temporada Episódio Final": UM episódio cujo número o nome não
        # diz. Pack ele não é; "unknown" o deixa fora do plano automático
        # (atribuível só à mão). "Completa" no nome manda: é o pack que
        # menciona um episódio ("Temporada 1 Completa + Episódio Especial").
        if _UNNUMBERED_EP_RE.search(title) and not _COMPLETE_RE.search(title):
            return Coverage("unknown")
        return Coverage("season_pack", seasons)
    if _COMPLETE_RE.search(title):
        return Coverage("complete")
    return Coverage("unknown")


def strip_episode_tokens(title: str) -> str:
    """Remove tokens de episódio/temporada do nome, sobrando o título da série
    (+ tags de qualidade) — para casar contra o título do TMDB com o matching
    de filmes (todas as palavras)."""
    # episódio por extenso: só os trechos que de fato viraram referência —
    # o número do episódio não pode contar como palavra do título ("Serie 8"
    # casaria "Outra Serie 2ª Temporada Episódio 8")
    refs, spans = _word_episode_refs(title)
    t = _blank(title, spans) if refs else title
    t = _SXXEYY_RE.sub(" ", t)
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
