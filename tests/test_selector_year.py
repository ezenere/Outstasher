"""Bônus de ano no modo áudio: release dublado com o ano do filme sobe na fila."""
from services import selector


def cand(title, seeders=50, size=8_000_000_000):
    return {"title": title, "seeders": seeders, "size": size,
            "magnet": f"magnet:?xt=urn:btih:{abs(hash(title)):x}", "tracker": "t"}


def test_has_year_matches_common_formats():
    assert selector.has_year("Ex Machina (2014) Dublado 1080p", "2014")
    assert selector.has_year("Ex.Machina.2014.DUAL.1080p", "2014")
    assert selector.has_year("Ex Machina 2014 Dublado", "2014")


def test_has_year_ignores_embedded_digits():
    assert not selector.has_year("Ex Machina Dublado 1080p", "2014")
    assert not selector.has_year("Ex Machina 32014 Dublado", "2014")  # 2014 dentro de 32014
    assert not selector.has_year("Ex Machina 20145 Dublado", "2014")  # prefixo de outro número
    assert not selector.has_year("Ex Machina Dublado", "")            # sem ano de referência


def test_audio_year_ranks_first(temp_db):
    results = [cand("Ex Machina Dublado 1080p WEB-DL"),
               cand("Ex Machina (2014) Dublado 1080p WEB-DL")]
    ranked, _ = selector.rank(results, "audio", "Ex Machina", "2014", language="pt")
    assert ranked[0]["title"] == "Ex Machina (2014) Dublado 1080p WEB-DL"
    diff = ranked[0]["score"] - ranked[1]["score"]
    assert abs(diff - selector.YEAR_BONUS) < 0.2, diff  # a diferença é exatamente o bônus


def test_year_bonus_does_not_override_much_better_release(temp_db):
    results = [cand("Ex Machina (2014) Dublado HDTV 480p", seeders=5),
               cand("Ex Machina Dublado BluRay 1080p TrueHD", seeders=200)]
    ranked, _ = selector.rank(results, "audio", "Ex Machina", "2014", language="pt")
    assert ranked[0]["title"].startswith("Ex Machina Dublado BluRay")


def test_video_mode_has_no_year_bonus(temp_db):
    # a busca do original já vai com o ano na query, então vídeo não ganha bônus
    results = [cand("Ex Machina 1080p BluRay x264"),
               cand("Ex Machina 2014 1080p BluRay x264")]
    ranked, _ = selector.rank(results, "video", "Ex Machina", "2014")
    assert abs(ranked[0]["score"] - ranked[1]["score"]) < 0.2


def test_year_bonus_visible_in_trace(temp_db):
    results = [cand("Filme Dublado 720p"), cand("Filme 2014 Dublado 720p")]
    _, trace = selector.rank(results, "audio", "Filme", "2014", language="pt")
    by_title = {c["title"]: c for c in trace}
    s_com = by_title["Filme 2014 Dublado 720p"]["score"]
    s_sem = by_title["Filme Dublado 720p"]["score"]
    assert s_com == s_sem + selector.YEAR_BONUS
