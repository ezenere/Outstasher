"""Ano no modo áudio: release com o ano do filme tem preferência ABSOLUTA.

Título sem ano é ambíguo (um título de franquia casa com todas as sequências),
então todo release com o ano vem antes de qualquer um sem ano — o score só
ordena dentro de cada grupo.
"""
from services import selector


def cand(title, seeders=50, size=8_000_000_000):
    return {"title": title, "seeders": seeders, "size": size,
            "magnet": f"magnet:?xt=urn:btih:{abs(hash(title)):x}", "tracker": "t"}


def test_has_year_matches_common_formats():
    assert selector.has_year("Filme Exemplo (2014) Dublado 1080p", "2014")
    assert selector.has_year("Filme.Exemplo.2014.DUAL.1080p", "2014")
    assert selector.has_year("Filme Exemplo 2014 Dublado", "2014")


def test_has_year_ignores_embedded_digits():
    assert not selector.has_year("Filme Exemplo Dublado 1080p", "2014")
    assert not selector.has_year("Filme Exemplo 32014 Dublado", "2014")  # 2014 dentro de 32014
    assert not selector.has_year("Filme Exemplo 20145 Dublado", "2014")  # prefixo de outro número
    assert not selector.has_year("Filme Exemplo Dublado", "")            # sem ano de referência


def test_audio_year_ranks_first(temp_db):
    results = [cand("Filme Exemplo Dublado 1080p WEB-DL"),
               cand("Filme Exemplo (2014) Dublado 1080p WEB-DL")]
    ranked, _ = selector.rank(results, "audio", "Filme Exemplo", "2014", language="pt")
    assert ranked[0]["title"] == "Filme Exemplo (2014) Dublado 1080p WEB-DL"
    assert ranked[0]["year_match"] and not ranked[1]["year_match"]


def test_year_beats_higher_score(temp_db):
    # preferência ABSOLUTA: o release com ano vence mesmo com score bem menor
    # (sem o ano, um título de franquia pode ser qualquer sequência)
    results = [cand("Herois da Franquia (2014) Dublado HDTV 480p", seeders=5),
               cand("Herois da Franquia Dublado BluRay 1080p TrueHD", seeders=200)]
    ranked, _ = selector.rank(results, "audio", "Herois da Franquia", "2014",
                              language="pt")
    assert ranked[0]["title"].startswith("Herois da Franquia (2014)")
    assert ranked[0]["score"] < ranked[1]["score"]  # o ano venceu apesar do score


def test_score_orders_within_year_group(temp_db):
    # dentro do grupo com ano, o score continua decidindo
    results = [cand("Filme 2014 Dublado HDTV 480p", seeders=5),
               cand("Filme 2014 Dublado BluRay 1080p TrueHD", seeders=200)]
    ranked, _ = selector.rank(results, "audio", "Filme", "2014", language="pt")
    assert ranked[0]["title"] == "Filme 2014 Dublado BluRay 1080p TrueHD"


def test_video_mode_ignores_year(temp_db):
    # a busca do original já vai com o ano na query, então vídeo ordena só por
    # score — o release melhor vence mesmo sem o ano no nome
    results = [cand("Filme Exemplo 2014 HDTV 480p", seeders=5),
               cand("Filme Exemplo 1080p BluRay x264", seeders=200)]
    ranked, _ = selector.rank(results, "video", "Filme Exemplo", "2014")
    assert ranked[0]["title"] == "Filme Exemplo 1080p BluRay x264"


def test_rank_matches_any_alternative_title(temp_db):
    # filme estrangeiro: releases vêm com o nome original OU o nome em inglês —
    # rank aceita uma lista e casa QUALQUER um dos dois
    results = [cand("Tale of the Wind 2001 1080p BluRay x264"),
               cand("Kaze no Monogatari 2001 2160p BluRay REMUX"),
               cand("Filme Aleatorio 2001 1080p")]
    titles = ["Kaze no Monogatari", "Tale of the Wind"]
    ranked, _ = selector.rank(results, "video", titles, "2001")
    got = {r["title"] for r in ranked}
    assert "Tale of the Wind 2001 1080p BluRay x264" in got          # nome inglês
    assert "Kaze no Monogatari 2001 2160p BluRay REMUX" in got  # nome original
    assert "Filme Aleatorio 2001 1080p" not in got               # nenhum dos dois


def test_rank_single_title_still_works(temp_db):
    # retrocompat: passar uma STRING (não lista) continua casando normalmente
    results = [cand("Filme Exemplo 2014 1080p BluRay")]
    ranked, _ = selector.rank(results, "video", "Filme Exemplo", "2014")
    assert len(ranked) == 1


def test_year_match_visible_in_trace(temp_db):
    # o trace expõe year_match (a UI marca com a tag [ano]) e ordena os viáveis
    # com ano antes dos sem ano
    results = [cand("Filme Dublado BluRay 1080p", seeders=200),
               cand("Filme 2014 Dublado 720p", seeders=5)]
    _, trace = selector.rank(results, "audio", "Filme", "2014", language="pt")
    by_title = {c["title"]: c for c in trace}
    assert by_title["Filme 2014 Dublado 720p"]["year_match"] is True
    assert by_title["Filme Dublado BluRay 1080p"]["year_match"] is False
    assert trace[0]["title"] == "Filme 2014 Dublado 720p"
