"""Episódio POR EXTENSO no nome do release ("9ª Temporada 08º Episódio").

Indexadores brasileiros nomeiam avulsos sem SxxEyy: a temporada e o episódio
vêm escritos. Antes o parser só via a temporada e o avulso virava pack — caso
real: três avulsos "9ª Temporada 08º/09º/10º EPISÓDIO" apareceram como "S09
completa" na escolha manual e cada um assumiu os três episódios pedidos.

Corpus com nomes fictícios reproduzindo as formas que aparecem nos
indexadores, e as armadilhas (resolução, ano, codec, áudio 5.1, contagem de
episódios do pack, "Episódio Final", "ep" dentro de palavra, episódio sem
temporada).
"""
import pytest

from services.series import parse

# o formato do caso real, com título fictício
REAL = "Desenhos Serie Exemplo 9ª Temporada {:02d}º EPISÓDIO Dubbed Dual 5.1 WEB-DL 1080P"


# -------------------- um episódio --------------------

EPISODIO = [
    # ordinal antes da palavra (a forma do caso real)
    (REAL.format(8), (9, 8)),
    (REAL.format(10), (9, 10)),
    ("Serie Exemplo 9ª Temporada 08º Episódio 1080p", (9, 8)),
    ("Serie Exemplo 9ª Temporada 8º Episodio 720p", (9, 8)),
    ("Serie Exemplo 9ª Temporada 8º EP WEB-DL", (9, 8)),
    ("Serie Exemplo 9ª Temporada 8° Ep. Dublado", (9, 8)),
    ("Serie Exemplo 9a temporada 8o episodio nacional", (9, 8)),
    ("Serie Exemplo 9ª Temporada 8ª Episódio", (9, 8)),
    ("Serie Exemplo 2ª Temporada 3.º Episódio", (2, 3)),
    ("serie exemplo 9ª temporada 08º episódio", (9, 8)),
    ("SERIE EXEMPLO 9ª TEMPORADA 08º EPISODIO", (9, 8)),
    ("Serie Exemplo 2ª Temporada 08 Episódio", (2, 8)),   # sem ordinal, singular
    ("Serie.Exemplo.9a.Temporada.08o.Episodio.720p", (9, 8)),
    # palavra antes do número
    ("Serie Exemplo Temporada 9 Episódio 08 1080p", (9, 8)),
    ("Serie Exemplo Temporada 9 Episodio 8 1080p", (9, 8)),
    ("Serie Exemplo 9ª Temporada EPISÓDIO 08", (9, 8)),
    ("Serie Exemplo Temporada 9 Ep 08 - 1080p", (9, 8)),
    ("Serie Exemplo Temporada 9 Ep. 08 x264", (9, 8)),
    ("Serie Exemplo Temporada 9 Ep.08 WEB-DL", (9, 8)),
    ("Serie Exemplo 9ª Temporada EP08 Dublado", (9, 8)),
    ("Serie Exemplo 9ª Temporada Ep-08", (9, 8)),
    ("Serie Exemplo 2ª Temporada Episódio Nº 3", (2, 3)),
    ("Serie Exemplo 2ª Temporada Episode #3", (2, 3)),
    ("Serie Exemplo 2ª Temporada [Episódio 03]", (2, 3)),
    ("Serie Exemplo 2ª Temporada (Ep.03)", (2, 3)),
    ("Serie Exemplo 2ª Temporada Capítulo 3", (2, 3)),
    ("Serie Exemplo - 2ª Temporada - Episódio 05 [1080p]", (2, 5)),
    ("Serie Exemplo Temporada 2 Episodio 08 de 10", (2, 8)),  # "8 de 10"
    ("Serie Exemplo Temporada 2 Episódio 05 a Volta", (2, 5)),  # "a" sem número
    ("Serie Exemplo Temporada 0 Episodio 0", (0, 0)),  # especial
    # inglês
    ("Serie Exemplo Season 9 Episode 8 2026 1080p", (9, 8)),
    ("Serie.Exemplo.Season.9.Episode.8.720p", (9, 8)),
    ("Serie Exemplo Season 9 - Ep. 8", (9, 8)),
    # "E08" solto: só porque há temporada por extenso no nome
    ("Serie Exemplo 9ª Temporada E08 Dublado", (9, 8)),
    ("Serie Exemplo Temporada 9 E8 1080p", (9, 8)),
    # temporada como "S09" solto + episódio por extenso
    ("Serie Exemplo S09 Episodio 08", (9, 8)),
    ("Serie Exemplo S9 Ep 8", (9, 8)),
    ("Serie.Exemplo.S09.EP08.1080p", (9, 8)),
    # episódio escrito ANTES da temporada
    ("Serie Exemplo Episódio 08 Temporada 9", (9, 8)),
    ("Serie Exemplo 08º Episódio da 9ª Temporada", (9, 8)),
    # o lixo do Jackett no lugar do "ª"/"º"/"Ó"
    ("Serie Exemplo 9� Temporada 08� EPIS�DIO Dual", (9, 8)),
    ("Serie Exemplo 9�� Temporada 08�� EPIS��DIO", (9, 8)),
    ("Serie Exemplo 9Âª Temporada 08Âº EPISÃ“DIO Dual", (9, 8)),
    # números de resolução/ano/codec/áudio por perto não viram episódio
    ("Serie Exemplo Temporada 9 Episódio 8 720p", (9, 8)),
    ("Serie Exemplo Temporada 9 Episódio 8 10bit", (9, 8)),
    ("Serie Exemplo Temporada 9 Dual 5.1 Episódio 8", (9, 8)),
    ("Serie Exemplo Temporada 9 x264 Episódio 8", (9, 8)),
    ("Serie Exemplo 2026 Temporada 9 Episódio 8 2.0", (9, 8)),
    ("Serie Exemplo Temporada 9 Ep 08, 2 Audios", (9, 8)),
    ("Serie Exemplo Temporada 2 EPISÓDIO 05 1080p e 720p", (2, 5)),
]


@pytest.mark.parametrize("title,ref", EPISODIO)
def test_episodio_por_extenso(title, ref):
    c = parse.parse_coverage(title)
    assert c.kind == "episode", title
    assert c.episodes == [ref], title
    assert c.covers(*ref)
    # cobre SÓ o próprio episódio, não a temporada inteira
    assert not c.covers(ref[0], ref[1] + 1)
    assert c.label() == f"S{ref[0]:02d}E{ref[1]:02d}"


def test_caso_real_tres_avulsos_cada_um_o_seu():
    covs = [parse.parse_coverage(REAL.format(n)) for n in (8, 9, 10)]
    assert [c.label() for c in covs] == ["S09E08", "S09E09", "S09E10"]
    for n, c in zip((8, 9, 10), covs):
        assert [e for e in (8, 9, 10) if c.covers(9, e)] == [n]


# -------------------- vários episódios --------------------

MULTI = [
    ("Serie Exemplo 9ª Temporada Episódios 01 a 05", 9, [1, 2, 3, 4, 5]),
    ("Serie Exemplo 9ª Temporada Episodios 01 ao 05 1080p", 9, [1, 2, 3, 4, 5]),
    ("Serie Exemplo 9ª Temporada Episódios 1 até 3", 9, [1, 2, 3]),
    ("Serie Exemplo 9ª Temporada EP 01-05", 9, [1, 2, 3, 4, 5]),
    ("Serie Exemplo Temporada 2 Ep 01 ~ 03", 2, [1, 2, 3]),
    ("Serie Exemplo Temporada 2 Ep01-Ep03", 2, [1, 2, 3]),
    ("Serie Exemplo Temporada 2 Eps 4-6", 2, [4, 5, 6]),
    ("Serie Exemplo 9ª Temporada E08-E10 Dublado", 9, [8, 9, 10]),
    ("Serie Exemplo 9ª Temporada 01º ao 05º Episódios", 9, [1, 2, 3, 4, 5]),
    ("Serie Exemplo 9ª Temporada 1º e 2º Episódios", 9, [1, 2]),
    # arquivo duplo / lista
    ("Serie Exemplo 9ª Temporada Episódios 01 e 02", 9, [1, 2]),
    ("Serie Exemplo Temporada 2 Episódio 1, 2 e 3", 2, [1, 2, 3]),
    ("Serie Exemplo Temporada 2 Ep 08 e Ep 09", 2, [8, 9]),
    ("Serie Exemplo Season 2 Episodes 1 to 5", 2, [1, 2, 3, 4, 5]),
    ("Serie Exemplo Season 2 Episodes 1-3 & 5", 2, [1, 2, 3, 5]),
    # "Temporada N Episódios 1-10" é range explícito: cobre o que diz (se a
    # temporada tiver mais episódios, o resto não está no torrent)
    ("Serie Exemplo 9ª Temporada Episódios 1-10", 9, list(range(1, 11))),
    ("Serie Exemplo Temporada 9 Completa Episódios 01 ao 10", 9, list(range(1, 11))),
]


@pytest.mark.parametrize("title,season,eps", MULTI)
def test_varios_episodios_por_extenso(title, season, eps):
    c = parse.parse_coverage(title)
    assert c.kind == "multi_episode", title
    assert c.episodes == [(season, e) for e in eps], title
    assert not c.covers(season, max(eps) + 1)


def test_varias_temporadas_cada_episodio_na_sua():
    c = parse.parse_coverage(
        "Serie Exemplo Temporada 1 Episódios 1-3 e Temporada 2 Episódios 1-2")
    assert c.episodes == [(1, 1), (1, 2), (1, 3), (2, 1), (2, 2)]


def test_range_invertido_fica_so_o_primeiro():
    # como "S01E05-02": range decrescente não é range
    assert parse.parse_episode_refs("Serie Exemplo Temporada 2 Ep 05-02") == [(2, 5)]


# -------------------- continua pack / série / desconhecido --------------------

PACK = [
    ("Serie Exemplo Temporada 9 Completa", {9}),
    ("Serie Exemplo 9ª Temporada Completa 1080p Dual 5.1", {9}),
    # contagem de episódios do pack, não o episódio N
    ("Serie Exemplo Temporada 9 Completa 10 Episódios", {9}),
    ("Serie Exemplo Temporada 1 - 10 Episódios", {1}),
    ("Serie Exemplo 2ª Temporada 10 EPs", {2}),
    ("Serie Exemplo 2ª Temporada Todos os Episódios", {2}),
    # "EP" curto sem ordinal pode ser contagem: não arrisca
    ("Serie Exemplo 2ª Temporada 08 EP", {2}),
    # palavra de episódio sem número utilizável
    ("Serie Exemplo Temporada 2 Episodio 1080p", {2}),
    ("Serie Exemplo Temporada 2 Ep 720p", {2}),
    ("Serie Exemplo Temporada 9 Ep 5.1 Dual", {9}),
    ("Serie Exemplo Temporada 2 Ep 2026", {2}),
    ("Serie Exemplo 2ª Temporada Ep 9ª", {2}),  # ª é de temporada
    # "ep" dentro de palavra / palavra parecida
    ("Mundo Deep 2ª Temporada 1080p", {2}),
    ("Mundo Deep 08 2ª Temporada 1080p", {2}),
    ("Serie Exemplo 2ª Temporada Epílogo 1080p", {2}),
    ("Serie Exemplo 2ª Temporada E-AC3 1080p", {2}),
    ("Serie Exemplo Temporada 2 DDP5.1 E 1080p", {2}),
    ("Serie Exemplo 2ª Temporada 1ª e 2ª Parte", {2}),
    # "Completa" manda: é o pack que menciona um episódio
    ("Serie Exemplo Temporada 1 Completa + Episódio Especial", {1}),
    # temporadas em range: não dá para saber de qual é o episódio (como antes)
    ("Serie Exemplo Temporadas 1 a 3 Episódio 5", {1, 2, 3}),
]


@pytest.mark.parametrize("title,seasons", PACK)
def test_continua_pack(title, seasons):
    c = parse.parse_coverage(title)
    assert c.kind == "season_pack", title
    assert c.seasons == seasons, title
    assert parse.parse_episode_refs(title) == []


SEM_NUMERO = [
    # UM episódio cujo número o nome não diz: não é pack da temporada
    "Serie Exemplo 9ª Temporada Episódio Final 1080p",
    "Serie Exemplo 9ª Temporada EPISODIO FINAL Dublado",
    "Serie Exemplo Season 9 Final Episode",
    "Serie Exemplo 2ª Temporada Último Episódio",
    "Serie Exemplo 1ª Temporada Episódio Piloto",
    "Serie Exemplo Season 1 Pilot Episode",
    "Serie Exemplo 2ª Temporada Episódio Especial",
    "Serie Exemplo Season 2 Season Finale 1080p",
]


@pytest.mark.parametrize("title", SEM_NUMERO)
def test_um_episodio_sem_numero_nao_e_pack(title):
    c = parse.parse_coverage(title)
    assert c.kind == "unknown", title
    assert not c.covers(9, 10) and not c.covers(2, 1)


def test_episodio_final_numerado_e_o_episodio():
    c = parse.parse_coverage("Serie Exemplo 9ª Temporada 10º Episódio Final")
    assert c.kind == "episode" and c.episodes == [(9, 10)]


SEM_TEMPORADA = [
    # episódio sem temporada no nome: a numeração pode ser absoluta (anime,
    # novela); não chuta temporada — fica sem referência, como "- 015"
    "Serie Exemplo Ep. 8 1080p",
    "Serie Exemplo Episódio 08 Dublado",
    "Serie Exemplo 08º Episódio",
    "Serie Exemplo EP08",
    "Serie Exemplo E05 1080p",
    "Anime Longo Ep 1042 1080p",
    "Serie Exemplo Episode 8",
]


@pytest.mark.parametrize("title", SEM_TEMPORADA)
def test_episodio_sem_temporada_fica_sem_referencia(title):
    assert parse.parse_episode_refs(title) == []
    assert parse.parse_coverage(title).kind == "unknown"


def test_filme_com_episodio_no_titulo_nao_vira_serie():
    assert parse.parse_episode_refs("Saga Exemplo Episódio 4 Uma Nova Era 1080p") == []


# -------------------- precedência e formas antigas --------------------

def test_sxxeyy_vence_por_extenso():
    # nome com as duas formas: a compacta manda (é a mais confiável)
    assert parse.parse_episode_refs("Serie Exemplo S02E05 Episódio 07") == [(2, 5)]
    assert parse.parse_episode_refs("Serie Exemplo 1x05 Temporada 1 Episodio 7") == [(1, 5)]


def test_formas_antigas_inalteradas():
    assert parse.parse_coverage("Show 1ª Temporada Dublado").kind == "season_pack"
    assert parse.parse_coverage("Show Temporadas 1 a 4 Dublado").seasons == {1, 2, 3, 4}
    assert parse.parse_coverage("Robo T2 1080p").kind == "unknown"
    assert parse.parse_coverage("Show S01E01-03").episodes == [(1, 1), (1, 2), (1, 3)]


def test_nome_de_arquivo_por_extenso():
    # parse_episode_refs também lê os ARQUIVOS do pack (seleção de arquivos,
    # legendas, origem manual): o arquivo por extenso agora é reconhecido
    name = "Serie Exemplo 9ª Temporada 08º Episódio Dublado.mkv"
    assert parse.parse_episode_refs(name) == [(9, 8)]
    assert parse.parse_episode_refs("Episódio 08.mkv") == []  # sem temporada


# -------------------- strip_episode_tokens --------------------

def test_strip_remove_episodio_por_extenso():
    assert parse.strip_episode_tokens(REAL.format(8)) == \
        "Desenhos Serie Exemplo Dubbed Dual 5.1 WEB-DL 1080P"
    assert parse.strip_episode_tokens(
        "Serie Exemplo Temporada 2 Episódios 01 a 05 720p") == "Serie Exemplo 720p"


def test_strip_numero_do_episodio_nao_conta_como_titulo():
    # "Serie 8" não pode casar a série "Outra Serie" pelo número do episódio
    from services import selector
    bare = parse.strip_episode_tokens("Outra Serie 2ª Temporada Episódio 8 1080p")
    assert not selector.matches_title(bare, "Serie 8")
    assert selector.matches_title(bare, "Outra Serie")
