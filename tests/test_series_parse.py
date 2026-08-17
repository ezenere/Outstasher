"""Parsing de nomes de release de TV: episódios, packs, sinais de suspeita.

Corpus baseado em nomes reais de release (scene/P2P/BR), incluindo os formatos
que mais quebram parser ingênuo: ranges, "1x02", packs multi-resolução,
numeração absoluta de anime e episódios por data.
"""
from services.series import parse


# -------------------- parse_episode_refs --------------------

def test_sxxeyy_basico():
    assert parse.parse_episode_refs("Breaking.Bad.S01E02.720p.HDTV") == [(1, 2)]
    assert parse.parse_episode_refs("show s1e2") == [(1, 2)]
    assert parse.parse_episode_refs("Show S01.E02.mkv") == [(1, 2)]
    assert parse.parse_episode_refs("Show S01_E02") == [(1, 2)]
    assert parse.parse_episode_refs("Show S01 E02") == [(1, 2)]


def test_txxeyy_convencao_brasileira():
    # caso real: pack "By Lucas Firmo" com arquivos "T01E01 - olámigo.mov.mkv"
    assert parse.parse_episode_refs("T01E01 - olámigo.mov.mkv") == [(1, 1)]
    assert parse.parse_episode_refs("Show t02e11 720p") == [(2, 11)]
    # "T2" solto NÃO vira temporada (poderia ser "Terminator 2")
    assert parse.parse_coverage("Terminator T2 1080p").kind == "unknown"


def test_sxxeyy_range():
    assert parse.parse_episode_refs("Show S01E01-E03 1080p") == [(1, 1), (1, 2), (1, 3)]
    assert parse.parse_episode_refs("Show S01E01-03") == [(1, 1), (1, 2), (1, 3)]
    assert parse.parse_episode_refs("Show S01E05~E06") == [(1, 5), (1, 6)]
    # range invertido não é range: fica só o primeiro episódio
    assert parse.parse_episode_refs("Show S01E05-02") == [(1, 5)]


def test_multiplos_episodios():
    assert parse.parse_episode_refs("Show S02E01 S02E02") == [(2, 1), (2, 2)]
    # duplicata some, ordem preservada
    assert parse.parse_episode_refs("Show S02E01 (S02E01)") == [(2, 1)]


def test_formato_1x02():
    assert parse.parse_episode_refs("Show 1x02 HDTV") == [(1, 2)]
    assert parse.parse_episode_refs("Show.3x11.720p") == [(3, 11)]


def test_1x02_nao_casa_codec_nem_resolucao():
    # x264/x265 e 1920x1080 são as armadilhas clássicas do formato NxMM
    assert parse.parse_episode_refs("Show 2026 1080p x264") == []
    assert parse.parse_episode_refs("Show 5.1 x265 10bit") == []
    assert parse.parse_episode_refs("Show 1920x1080 WEB-DL") == []
    assert parse.parse_episode_refs("Show.1x264") == []


def test_sem_referencia():
    assert parse.parse_episode_refs("Breaking Bad 1080p BluRay") == []


# -------------------- parse_coverage --------------------

def test_coverage_episodio():
    c = parse.parse_coverage("Show S01E02 1080p")
    assert c.kind == "episode"
    assert c.covers(1, 2)
    assert not c.covers(1, 3)
    assert c.label() == "S01E02"


def test_coverage_season_pack():
    for title in ("Show S01 1080p BluRay", "Show Season 1 Complete 720p",
                  "Show Temporada 1 WEB-DL", "Show 1ª Temporada Dublado",
                  "Show 3a temporada nacional"):
        c = parse.parse_coverage(title)
        assert c.kind == "season_pack", title
        season = 3 if "3a" in title else 1
        assert c.covers(season, 7), title
        assert not c.covers(9, 1), title


def test_coverage_multi_season():
    c = parse.parse_coverage("Show S01-S03 1080p")
    assert c.kind == "season_pack"
    assert c.seasons == {1, 2, 3}
    c = parse.parse_coverage("Show Seasons 1-2 Complete")
    assert c.seasons == {1, 2}
    c = parse.parse_coverage("Show Temporadas 1 a 4 Dublado")
    assert c.seasons == {1, 2, 3, 4}


def test_coverage_complete():
    c = parse.parse_coverage("Show Complete Series 1080p")
    assert c.kind == "complete"
    # "complete" cobre o que existir na série (known_seasons do TMDB)
    assert c.covers(4, 2, known_seasons={1, 2, 3, 4})
    assert not c.covers(9, 1, known_seasons={1, 2, 3, 4})
    assert parse.parse_coverage("Show Coleção Completa").kind == "complete"
    assert parse.parse_coverage("Show [Batch] 1080p").kind == "complete"


def test_coverage_episodio_ganha_de_pack():
    # nome com S01E02: é UM episódio, mesmo que "S01" apareça no token
    c = parse.parse_coverage("Show S01E02 de S01 1080p")
    assert c.kind == "episode"


def test_coverage_unknown():
    assert parse.parse_coverage("Show 1080p BluRay").kind == "unknown"


# -------------------- strip_episode_tokens --------------------

def test_strip_tokens():
    assert "S01E02" not in parse.strip_episode_tokens("Breaking Bad S01E02 720p")
    assert "Temporada" not in parse.strip_episode_tokens("Show 1ª Temporada 720p")
    stripped = parse.strip_episode_tokens("Breaking Bad S01E02 720p HDTV")
    assert "Breaking Bad" in stripped and "720p" in stripped


# -------------------- pack_resolution_tier --------------------

def test_pack_multi_resolucao_usa_a_melhor():
    # selector.quality_tier daria resolução desconhecida; o pack usa a melhor
    tier_pack, label = parse.pack_resolution_tier("Show S01 1080p 720p WEB-DL")
    tier_1080, _ = parse.pack_resolution_tier("Show S01 1080p WEB-DL")
    assert tier_pack == tier_1080
    assert "pack misto" in label


def test_pack_resolucao_unica_igual_ao_selector():
    from services import selector
    t = "Show S01 1080p BluRay"
    assert parse.pack_resolution_tier(t) == selector.quality_tier(t)


# -------------------- torrent_identity --------------------

def test_identity_prioriza_hash():
    a = {"infohash": "ABC123", "magnet": "magnet:?xt=urn:btih:" + "f" * 40}
    b = {"magnet": "magnet:?xt=urn:btih:" + "F" * 40}
    c = {"link": "http://jackett/dl?file=x.torrent&path=efemero"}
    d = {"link": "http://jackett/dl/abc?path=efemero"}
    assert parse.torrent_identity(a) == "hash:abc123"
    assert parse.torrent_identity(b) == "hash:" + "f" * 40
    assert parse.torrent_identity(c) == "file:x.torrent"
    assert parse.torrent_identity(d) == "link:http://jackett/dl/abc"
    # mesmo hash em maiúscula/minúscula = mesmo torrent
    assert parse.torrent_identity(b) == parse.torrent_identity(
        {"magnet": "magnet:?xt=urn:btih:" + "f" * 40})


# -------------------- suspicious_signals --------------------

def test_sinal_numeracao_absoluta():
    sigs = parse.suspicious_signals("One Piece - 1042 [1080p]")
    assert any("absoluta" in s for s in sigs)
    # com SxxEyy no nome não há ambiguidade
    assert not parse.suspicious_signals("One Piece S20E12 1080p")


def test_sinal_data():
    sigs = parse.suspicious_signals("The Daily Show 2026.08.14 720p")
    assert any("data" in s for s in sigs)


def test_sinal_ordem_explicita():
    sigs = parse.suspicious_signals("Firefly S01 DVD Order 1080p")
    assert any("ordem" in s.lower() for s in sigs)


def test_sinal_pack_com_menos_arquivos():
    sigs = parse.suspicious_signals("Show S01 1080p", files_count=8,
                                    tmdb_episode_count=13)
    assert any("8 arquivo" in s for s in sigs)
    # mais arquivos que episódios (extras/samples) não é sinal
    assert not parse.suspicious_signals("Show S01 1080p", files_count=20,
                                        tmdb_episode_count=13)


def test_sinal_ordens_alternativas():
    groups = [{"name": "DVD Order"}]
    sigs = parse.suspicious_signals("Show S01 1080p", alt_order_groups=groups)
    assert any("DVD Order" in s for s in sigs)


def test_release_normal_sem_sinais():
    assert parse.suspicious_signals("Breaking.Bad.S01E02.1080p.BluRay") == []
    assert parse.suspicious_signals("Show S01 Completa 1080p Dual Áudio") == []
