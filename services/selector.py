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

# -------------------- qualidade estilo Radarr --------------------
# A qualidade é (fonte, resolução) resolvida num TIER ordinal — a "escada". A
# ordenação é pelo tier primeiro (não por um score somado onde seeders/áudio
# empurrariam um 1080p acima de um 4K remux); o tamanho do arquivo desempata
# dentro do tier. Quem executa a ordenação é rank(); score() só expõe o tier
# como número para o trace/UI.

# resolução: cada padrão -> rótulo canônico. Ordem importa (mais específico
# primeiro). Comparado contra o título _fold() (sem acento, "_/./- " viram espaço).
RESOLUTION_PATTERNS = [
    (re.compile(r"\b(?:2160p|4k|uhd)\b", re.I), "2160p"),
    (re.compile(r"\b(?:1440p|2k)\b", re.I), "1440p"),
    (re.compile(r"\b1080p\b", re.I), "1080p"),
    (re.compile(r"\b1080i\b", re.I), "1080i"),
    (re.compile(r"\b720p\b", re.I), "720p"),
    (re.compile(r"\b(?:576p|480p|480i|sd)\b", re.I), "480p"),
]

# fonte: padrão -> rótulo canônico. Ordem importa (remux antes de bluray;
# web-dl antes de web; bdrip antes de bluray para não classificar rip como disco).
SOURCE_PATTERNS = [
    (re.compile(r"\bremux\b", re.I), "remux"),
    (re.compile(r"\b(?:bdrip|brrip)\b", re.I), "bdrip"),
    (re.compile(r"\b(?:blu-?ray|bd25|bd50|complete\s*bluray)\b", re.I), "bluray"),
    (re.compile(r"\b(?:web-?dl|web\s?dl|amzn|nf|dsnp|hmax|atvp|itunes|\bit\b)\b", re.I), "webdl"),
    (re.compile(r"\bwebrip\b", re.I), "webrip"),
    (re.compile(r"\bweb\b", re.I), "web"),
    (re.compile(r"\b(?:hdtv|pdtv|sdtv|dsr)\b", re.I), "hdtv"),
    (re.compile(r"\b(?:dvdrip|dvd)\b", re.I), "dvdrip"),
    (re.compile(r"\b(?:hdcam|cam-?rip|\bcam\b|telesync|\bts\b|hdts|telecine|\btc\b)\b", re.I), "cam"),
]

# upscale de IA: a resolução ANUNCIADA (ex.: 4K) é falsa — a fonte real é menor,
# só interpolada. Vale para filmes antigos "remasterizados" por IA. Detectado no
# título, rebaixa a resolução efetiva para não competir com 4K/1080p nativos.
AI_UPSCALE_RE = re.compile(
    r"\bai[ ._-]?(?:up(?:scale|scaled|scaling)|enhanced|remaster(?:ed)?)\b"
    r"|\b(?:up(?:scale|scaled)|enhanced)[ ._-]?(?:by[ ._-]?)?ai\b"
    r"|\bneural[ ._-]?(?:upscale|enhance)", re.I)

# quando a resolução é fruto de upscale de IA, ela conta como este degrau (abaixo
# de 1080p nativo: um upscale para "4K" tem qualidade real de fonte menor)
_AI_UPSCALE_RES_RANK = 2  # mesmo degrau de 720p (< 1080p, que é 3)


def is_ai_upscale(title: str) -> bool:
    """True se o título indica upscale/enhance por IA (resolução inflada)."""
    return bool(AI_UPSCALE_RE.search(_fold(title)))


# fontes de disco/web usam a resolução como degrau; TV/DVD/CAM são degraus
# fixos no fundo (independem da resolução anunciada).
_TIERED_SOURCES = ("remux", "bluray", "bdrip", "webdl", "webrip", "web")

# a ESCADA: (fonte, resolução) -> tier. Maior = melhor. Espelha a ordem do
# Radarr: remux no topo por resolução, depois bluray, depois web-dl/webrip...
# Resolução desconhecida numa fonte boa fica abaixo do mesmo source em 480p
# (não dá para confiar num release que nem diz a resolução).
_RES_RANK = {"2160p": 4, "1440p": 3, "1080p": 3, "1080i": 2, "720p": 2, "480p": 1, None: 0}
# dentro da MESMA resolução: remux (disco intacto) > bluray (encode do disco) >
# web-dl (stream sem re-encode) > webrip (re-encode do stream) > web (genérico)
# > bdrip/brrip (re-encode do disco, o pior dos "bons")
_SOURCE_RANK = {"remux": 6, "bluray": 5, "webdl": 4, "webrip": 3, "web": 2, "bdrip": 1}

# degraus fixos abaixo das fontes com resolução (valores negativos ficam no
# fundo da escada, mas ainda acima de "sem nada")
_FIXED_TIER = {"hdtv": 5, "dvdrip": 3, "cam": -100}
UNKNOWN_TIER = 1  # nem fonte nem resolução reconhecidas


def resolution_of(title: str) -> str | None:
    """Resolução canônica do título, ou None. Se o título anuncia MAIS DE UMA
    resolução distinta (ex.: '1080p' e '2160p' no mesmo nome — pacote/erro de
    release), retorna None: não dá para confiar em qual é a do arquivo."""
    folded = _fold(title)
    found = {label for pat, label in RESOLUTION_PATTERNS if pat.search(folded)}
    return found.pop() if len(found) == 1 else None


def source_of(title: str) -> str | None:
    """Fonte canônica do título (remux/bluray/webdl/...), ou None."""
    folded = _fold(title)
    for pat, label in SOURCE_PATTERNS:
        if pat.search(folded):
            return label
    return None


def quality_tier(title: str) -> tuple[int, str]:
    """(tier ordinal, rótulo legível) da qualidade do título — a escada Radarr.

    Maior tier = melhor. O rótulo ('4K Remux', '1080p WEB-DL', 'Desconhecida')
    aparece no trace/UI. CAM & cia. ficam no fundo (tier negativo)."""
    res = resolution_of(title)
    src = source_of(title)
    res_label = {"2160p": "4K", "1440p": "1440p", "1080p": "1080p",
                 "1080i": "1080i", "720p": "720p", "480p": "480p"}.get(res)
    src_label = {"remux": "Remux", "bluray": "BluRay", "bdrip": "BDRip",
                 "webdl": "WEB-DL", "webrip": "WEBRip", "web": "WEB",
                 "hdtv": "HDTV", "dvdrip": "DVDRip", "cam": "CAM"}.get(src)

    # upscale de IA: a resolução anunciada é interpolada, não nativa. Rebaixa o
    # degrau de resolução (só quando o anunciado é MAIOR que o degrau de upscale
    # — não "melhora" um 480p enhanced) e marca no rótulo.
    res_rank = _RES_RANK[res]
    upscaled = is_ai_upscale(title)
    if upscaled and res_rank > _AI_UPSCALE_RES_RANK:
        res_rank = _AI_UPSCALE_RES_RANK
    ai_tag = " (AI Upscale)" if upscaled else ""

    if src in _TIERED_SOURCES:
        # tier = degrau da resolução * 8 + degrau da fonte: a resolução manda, e
        # a fonte desempata dentro da mesma resolução (a escada Radarr é por
        # resolução primeiro dentro de disco/web).
        tier = res_rank * 8 + _SOURCE_RANK[src]
        label = " ".join(x for x in (res_label, src_label) if x) or "Desconhecida"
        return tier, label + ai_tag
    if src in _FIXED_TIER:
        return _FIXED_TIER[src], (src_label or "Desconhecida") + ai_tag
    # sem fonte reconhecida: usa só a resolução (rebaixada se upscale), se houver
    if res:
        return res_rank, (res_label or "Desconhecida") + ai_tag
    return UNKNOWN_TIER, "Desconhecida" + ai_tag


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

# CAM/TS e cia. têm tier muito negativo — abaixo disto o candidato é rejeitado
MIN_TIER = -50

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
    """_clean + sem acentos, para comparar títulos ('Tóquio' == 'Toquio').

    O "_" vira espaço: para o Python ele é caractere de PALAVRA, então `\\W+`
    não separa por ele e 'Ex_Machina' (grafia que o TMDB usa) viraria um token
    único que nenhum torrent contém. Trackers e TMDB trocam "_", "." e "-" por
    espaço livremente — aqui todos viram separador.
    """
    nfkd = unicodedata.normalize("NFKD", _clean(text))
    stripped = "".join(c for c in nfkd if not unicodedata.combining(c))
    return stripped.replace("_", " ")


# sequências de franquia: TMDB costuma usar romano ("De Volta para o Futuro II")
# e os releases BR usam arábico ("De Volta para o Futuro 2"). Estas funções
# alimentam tanto o matching (equiparar II==2) quanto as buscas extras.
_ROMAN = {"i": "1", "ii": "2", "iii": "3", "iv": "4", "v": "5", "vi": "6",
          "vii": "7", "viii": "8", "ix": "9", "x": "10", "xi": "11",
          "xii": "12", "xiii": "13"}
# romano solto (palavra inteira), sem casar o "I" de "IMAX" nem o "V" de "VS"
_ROMAN_RE = re.compile(r"(?<![\w])(x{0,1}(?:ix|iv|v?i{1,3}|v|x))(?![\w])", re.I)


def _roman_to_arabic(text: str) -> str:
    """Troca numerais romanos soltos por arábicos (preserva o resto do texto)."""
    return _ROMAN_RE.sub(lambda m: _ROMAN.get(m.group(1).lower(), m.group(1)), text)


def has_roman_numeral(title: str) -> bool:
    """True se o título tem um numeral romano solto (II, III, IV...)."""
    return _roman_to_arabic(title) != title


def _matches_movie(title: str, movie_title: str, year: str) -> bool:
    """Confere se o resultado parece ser do filme certo (todas as palavras do titulo).

    - Numerais romanos e arábicos são equiparados (II == 2), então
      'De Volta para o Futuro 2' casa com 'De Volta para o Futuro II'.
    - Números do título CONTAM (o '9' de 'Velozes & Furiosos 9' é obrigatório,
      senão a franquia inteira casa) e são comparados como PALAVRA inteira —
      substring casaria o '9' de '2019'/'x265', e o 'f9' do hash 'F98D9609'.
    - Palavras só de letras seguem por substring ('spider' casa 'spiderman').
    """
    folded = _roman_to_arabic(_fold(title))
    words = [w for w in re.split(r"\W+", _roman_to_arabic(_fold(movie_title)))
             if len(w) > 1 or w.isdigit()]
    for w in words:
        if any(ch.isdigit() for ch in w):
            # numero (9, 007) ou palavra com digito (f9, u2): palavra inteira
            if not re.search(rf"(?<![0-9a-z]){re.escape(w)}(?![0-9a-z])", folded):
                return False
        elif w not in folded:
            return False
    return True


def matches_title(title: str, movie_title: str) -> bool:
    """True se o nome do torrent contém o título (sem acentos/entidades HTML)."""
    return _matches_movie(title, movie_title, "")


def title_variants(title: str, include_and: bool = True) -> list[str]:
    """Variantes de grafia de caracteres especiais para buscas adicionais.

    Trackers grafam o mesmo filme de formas diferentes: "Velozes & Furiosos"
    vs "Velozes e Furiosos", "M*A*S*H" vs "MASH", "WALL·E" vs "WALL-E".
    Gera as variações trocando/removendo esses caracteres. Retorna só as
    variantes DIFERENTES do título original, sem duplicatas.

    include_and: se o "&" também vira "and". Ligado só para o título ORIGINAL
    (inglês); num título localizado em português "and" é ruído — usa-se "e".
    """
    if not title:
        return []
    base = title

    def apply(text: str, repls: list[tuple[str, str]]) -> str:
        for a, b in repls:
            text = text.replace(a, b)
        return re.sub(r"\s{2,}", " ", text).strip()

    # grafias possíveis do "&": "e" (pt), "and" (só no título original), removido.
    # Só convertemos "&" -> texto (não o inverso): o TMDB é canônico, então se
    # ele traz "e"/"and" é porque o nome é assim — forçar "&" só traria outro
    # filme que genuinamente usa "&".
    amp_options = [" e ", " "] + ([" and "] if include_and else [])
    variants: list[str] = []
    if "&" in base:
        for opt in amp_options:
            variants.append(apply(base, [("&", opt)]))

    # outros caracteres que os trackers costumam remover/normalizar
    misc = apply(base, [("@", "a"), ("+", " "), ("·", " "), ("*", " "),
                        ("’", "'"), ("“", ""), ("”", "")])
    if misc != base:
        variants.append(misc)
    # versão totalmente sem pontuação especial (mantém letras/números/espaço)
    stripped = re.sub(r"[^\w\s]", " ", base)
    stripped = re.sub(r"\s{2,}", " ", stripped).strip()
    if stripped:
        variants.append(stripped)

    # dedup preservando ordem, removendo o próprio título
    seen = {base.lower()}
    out = []
    for v in variants:
        vl = v.lower()
        if v and vl not in seen:
            seen.add(vl)
            out.append(v)
    return out


# Bônus (modo audio) para marcador forte: dublagem confirmada no idioma vence
# "dual"/"multi" genérico mesmo com release de qualidade um pouco menor.
STRONG_MARKER_BONUS = 25

# Modo audio: o ANO do filme no nome do release tem preferência ABSOLUTA.
# Releases dublados muitas vezes vêm sem o ano, e título sem ano é ambíguo:
# pode ser outro filme da franquia ("Guardiões da Galáxia" casa com Vol. 2 e 3)
# ou remake com o mesmo nome — o _matches_movie não distingue. Com o ano a
# identificação é confiável, então TODOS os releases com ano vêm antes de
# qualquer um sem ano; o score só ordena dentro de cada grupo.


def has_year(title: str, year: str) -> bool:
    """True se o nome do torrent contém o ano do filme como número isolado
    ('(2014)', '.2014.', ' 2014 ') — sem casar o '2014' de '32014' ou de um hash."""
    if not year:
        return False
    return bool(re.search(rf"(?<!\d){re.escape(year)}(?!\d)", _fold(title)))


def marker_strength(title: str, language: str, dubbed_title: str | None = None) -> int:
    """2 = marcador forte (dublagem confirmada), 1 = fraco ('dual'...), 0 = nenhum.

    dubbed_title: título do filme no idioma dublado, passado SÓ quando difere do
    original (senão não distingue nada). Se o release traz esse título junto de
    um marcador fraco ('dual'), promovemos a FORTE: título localizado + dual é
    indício forte de que a 2ª faixa é a dublagem no idioma alvo (um "dual"
    solto seria ambíguo — Hindi+English etc.).
    """
    t = " " + _clean(title) + " "
    info = config.LANGUAGES[language]
    if any(m in t for m in info["markers_strong"]):
        return 2
    if any(m in t for m in info["markers_weak"]):
        # título dublado (≠ original) + dual => dublagem no idioma alvo confirmada
        if dubbed_title and matches_title(title, dubbed_title):
            return 2
        return 1
    return 0


def has_language_marker(title: str, language: str, dubbed_title: str | None = None) -> bool:
    return marker_strength(title, language, dubbed_title) > 0


# marcadores de legenda como PALAVRA inteira (\b) — "leg"/"ost" nao podem casar
# dentro de "legiao"/"lost". Sem acento porque comparamos contra _fold().
# Compilado sob demanda e recacheado quando a lista muda (editavel pela UI).
_subs_cache: tuple[tuple[str, ...], "re.Pattern"] | None = None


def _subs_re() -> "re.Pattern":
    global _subs_cache
    markers = tuple(config.SUBTITLE_MARKERS)
    if _subs_cache is None or _subs_cache[0] != markers:
        pattern = (re.compile(r"\b(" + "|".join(re.escape(m) for m in markers) + r")\b", re.I)
                   if markers else re.compile(r"(?!x)x"))  # nunca casa se vazio
        _subs_cache = (markers, pattern)
    return _subs_cache[1]


def is_subs_only(title: str, language: str, dubbed_title: str | None = None) -> bool:
    """True se o título indica LEGENDA sem nenhuma indicação de dublagem/dual.

    Nesse caso o vídeo tem áudio ORIGINAL (só legendado) e não serve como faixa
    dublada — mesmo que o título esteja no idioma alvo.
    """
    if has_language_marker(title, language, dubbed_title):
        return False  # tem dublado/dual: legenda junto não desqualifica
    return bool(_subs_re().search(_fold(title)))


def _audio_score(title: str) -> int:
    """Qualidade do áudio anunciada no nome (TrueHD/Atmos/DTS...), 0 se nenhum.
    Desempate FINO dentro do mesmo tier+tamanho — não move de tier."""
    return _keyword_score(" " + _clean(title) + " ", AUDIO_SCORES)


def score(result: dict, mode: str, language: str | None = None,
          dubbed_title: str | None = None) -> float:
    """Score exposto no trace/UI. É o TIER de qualidade (a escada) com um
    ajuste pequeno de áudio/dublagem, só para o número não empatar entre
    releases do mesmo tier. A ordenação de verdade é por (tier, tamanho) em
    rank() — este número não decide sozinho o ranking."""
    tier, _label = quality_tier(result["title"])
    s = float(tier * 100)  # o tier domina a ordem de grandeza
    s += _audio_score(result["title"]) * (2 if mode == "audio" else 0.3)
    if mode == "audio" and language \
            and marker_strength(result["title"], language, dubbed_title) == 2:
        s += STRONG_MARKER_BONUS
    return s


def rank(results: list[dict], mode: str, movie_title, year: str,
         language: str | None = None,
         require_language: bool = False,
         required_edition: str | None = ANY_EDITION,
         dubbed_title: str | None = None) -> tuple[list[dict], list[dict]]:
    """Retorna (viáveis ordenados, trace de todos os avaliados).

    Ordenação estilo Radarr: TIER de qualidade (a escada remux 4k > bluray 4k >
    webdl 4k > ... > desconhecida) primeiro; dentro do mesmo tier, o TAMANHO do
    arquivo (maior = mais bitrate); e por fim áudio/seeders como desempate fino.
    No modo áudio, o ANO no nome e o marcador FORTE de dublagem têm preferência
    absoluta sobre a qualidade (identificação e idioma vêm antes da imagem).

    movie_title: título a casar OU lista de títulos alternativos (casa se
    QUALQUER um bater) — para filmes estrangeiros indexados tanto pelo nome
    original quanto pelo nome em inglês.
    required_edition: ANY_EDITION libera qualquer corte; None exige corte normal;
    uma string ("extended", ...) exige aquele corte — para as duas versões
    baixadas serem do MESMO corte e os áudios alinharem.
    dubbed_title: título no idioma dublado (passar só se ≠ do original) — junto
    de 'dual' vira marcador forte (ver marker_strength).
    """
    # aceita 1 título ou vários (nome original + nome em inglês); casa qualquer um
    titles = [movie_title] if isinstance(movie_title, str) else list(movie_title)
    pairs: list[tuple[dict, dict]] = []
    for r in results:
        edition = edition_of(r["title"])
        tier, quality = quality_tier(r["title"])
        # marcador forte (só áudio): dublagem confirmada vence qualidade de
        # imagem — 2 grupos acima de tudo que não tem, como o ano
        strong = (mode == "audio" and language
                  and marker_strength(r["title"], language, dubbed_title) == 2)
        cand = {
            "title": r["title"],
            "tracker": r.get("tracker"),
            "seeders": r["seeders"],
            "size": r["size"],
            "edition": edition,
            "quality": quality,
            "score": None,
            "rejected": None,
            "chosen": False,
        }
        if not (r.get("magnet") or r.get("link")):
            cand["rejected"] = "sem magnet/link"
        elif not any(_matches_movie(r["title"], t, year) for t in titles):
            cand["rejected"] = "título não bate"
        elif require_language and language and not has_language_marker(r["title"], language, dubbed_title):
            cand["rejected"] = f"sem marcador de idioma ({config.LANGUAGES[language]['label']})"
        elif mode == "audio" and language and is_subs_only(r["title"], language, dubbed_title):
            cand["rejected"] = "só legendado (áudio original, sem dublagem)"
        elif required_edition != ANY_EDITION and edition != required_edition:
            cand["rejected"] = (f"corte diferente da outra versão "
                                f"({edition or 'normal'} ≠ {required_edition or 'normal'})")
        elif r["seeders"] <= 0:
            cand["rejected"] = "sem seeders"
        elif tier <= MIN_TIER:
            cand["rejected"] = "qualidade muito baixa (CAM/TS)"
        else:
            cand["score"] = round(score(r, mode, language, dubbed_title), 1)
        # áudio dublado: ano no nome = identificação confiável -> preferência
        # absoluta (vídeo não precisa: a busca já vai com o ano)
        cand["year_match"] = mode == "audio" and has_year(r["title"], year)
        # chave de ordenação (só para viáveis): grupos absolutos primeiro
        # (ano, dublagem forte), depois a escada de qualidade, depois tamanho
        cand["_sort"] = (
            cand["year_match"], bool(strong), tier,
            r["size"] or 0,               # maior primeiro (0/desconhecido no fim)
            _audio_score(r["title"]),
            _seeders_score(r["seeders"]),
        )
        pairs.append((cand, r))

    viable = [(c, r) for c, r in pairs if c["rejected"] is None]
    viable.sort(key=lambda p: p[0]["_sort"], reverse=True)
    ranked = []
    for c, r in viable:
        item = dict(r)
        item["score"] = c["score"]
        item["edition"] = c["edition"]
        item["quality"] = c["quality"]
        item["year_match"] = c["year_match"]
        ranked.append(item)

    trace = sorted(
        (c for c, _ in pairs),
        key=lambda c: (not c["chosen"], c["rejected"] is not None,
                       tuple(-x for x in c["_sort"])))
    for c in pairs:  # _sort é interno: não vaza para o trace/JSON
        c[0].pop("_sort", None)
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
