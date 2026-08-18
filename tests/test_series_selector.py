"""Seleção de torrents de série: ranking por episódio + plano de cobertura.

A regra decidida: qualidade primeiro (mesma escada Radarr dos filmes); em
qualidade equivalente vence o torrent que cobre mais episódios pedidos (pack);
o plano reaproveita torrents já escolhidos em vez de baixar redundância.
"""
from services.series import selector_tv


def cand(title, seeders=50, size=2_000_000_000, tracker="t"):
    return {"title": title, "seeders": seeders, "size": size,
            "magnet": f"magnet:?xt=urn:btih:{abs(hash(title)):040x}"[:60],
            "link": None, "tracker": tracker}


def _rank(results, season, episode, mode="video", requested=None, **kw):
    ranked, trace = selector_tv.rank_for_episode(
        results, mode, "Breaking Bad", "2008", season, episode,
        requested=requested, **kw)
    return ranked, trace


# -------------------- ranking por episódio --------------------

def test_cobre_o_episodio_ou_rejeita():
    results = [
        cand("Breaking Bad S01E02 1080p WEB-DL"),
        cand("Breaking Bad S01E03 1080p WEB-DL"),
        cand("Breaking Bad S02 1080p WEB-DL"),
    ]
    ranked, trace = _rank(results, 1, 2)
    assert [r["title"] for r in ranked] == ["Breaking Bad S01E02 1080p WEB-DL"]
    rejected = {c["title"]: c["rejected"] for c in trace}
    assert "não cobre S01E02" in rejected["Breaking Bad S01E03 1080p WEB-DL"]
    assert "não cobre S01E02" in rejected["Breaking Bad S02 1080p WEB-DL"]


def test_pack_cobre_episodio():
    results = [cand("Breaking Bad S01 1080p BluRay")]
    ranked, _ = _rank(results, 1, 5)
    assert len(ranked) == 1
    assert ranked[0]["coverage"] == "S01 completa"


def test_serie_completa_cobre_com_known_seasons():
    results = [cand("Breaking Bad Complete Series 1080p")]
    ranked, _ = _rank(results, 4, 2, known_seasons={1, 2, 3, 4, 5})
    assert len(ranked) == 1
    ranked, _ = _rank(results, 9, 1, known_seasons={1, 2, 3, 4, 5})
    assert ranked == []


def test_titulo_de_outra_serie_rejeita():
    results = [cand("Better Call Saul S01E02 1080p WEB-DL")]
    _, trace = _rank(results, 1, 2)
    assert trace[0]["rejected"] == "título não bate"


def test_qualidade_primeiro():
    # avulso 1080p BluRay > pack 720p: qualidade nunca é sacrificada por pack
    results = [
        cand("Breaking Bad S01 720p WEB-DL"),
        cand("Breaking Bad S01E02 1080p BluRay"),
    ]
    ranked, _ = _rank(results, 1, 2, requested=[(1, 1), (1, 2), (1, 3)])
    assert ranked[0]["title"] == "Breaking Bad S01E02 1080p BluRay"


def test_pack_desempata_no_mesmo_tier():
    # mesmo tier (1080p WEB-DL): o pack cobre mais episódios pedidos -> vence
    results = [
        cand("Breaking Bad S01E02 1080p WEB-DL"),
        cand("Breaking Bad S01 1080p WEB-DL"),
    ]
    ranked, _ = _rank(results, 1, 2, requested=[(1, 1), (1, 2), (1, 3)])
    assert ranked[0]["title"] == "Breaking Bad S01 1080p WEB-DL"


def test_audio_exige_marcador_e_rejeita_legendado():
    results = [
        cand("Breaking Bad S01E02 1080p Dublado PT-BR"),
        cand("Breaking Bad S01E02 1080p Legendado"),
        cand("Breaking Bad S01E02 1080p"),
    ]
    ranked, trace = _rank(results, 1, 2, mode="audio", language="pt",
                          require_language=True)
    assert [r["title"] for r in ranked] == ["Breaking Bad S01E02 1080p Dublado PT-BR"]
    rejected = {c["title"]: c["rejected"] for c in trace}
    assert rejected["Breaking Bad S01E02 1080p"] is not None


def test_marcador_forte_vence_qualidade_no_audio():
    # dublagem confirmada (forte) vence 4K com marcador fraco — como em filmes
    results = [
        cand("Breaking Bad S01E02 2160p WEB-DL dual"),
        cand("Breaking Bad S01E02 720p WEB-DL Dublado"),
    ]
    ranked, _ = _rank(results, 1, 2, mode="audio", language="pt")
    assert ranked[0]["title"] == "Breaking Bad S01E02 720p WEB-DL Dublado"


def test_trace_tem_coverage_e_shape_de_filme():
    results = [cand("Breaking Bad S01 1080p WEB-DL")]
    _, trace = _rank(results, 1, 2)
    c = trace[0]
    # colunas que a CandidatesTable espera + a nova
    for key in ("title", "tracker", "seeders", "size", "quality", "score",
                "rejected", "chosen", "coverage"):
        assert key in c
    assert "_sort" not in c


# -------------------- build_plan --------------------

def _ranked_map(requested, results, **kw):
    out = {}
    for s, e in requested:
        ranked, _ = selector_tv.rank_for_episode(
            results, "video", "Breaking Bad", "2008", s, e,
            requested=requested, **kw)
        out[(s, e)] = ranked
    return out


def test_plan_reusa_pack():
    requested = [(1, 1), (1, 2), (1, 3)]
    results = [
        cand("Breaking Bad S01 1080p WEB-DL", size=9_000_000_000),
        cand("Breaking Bad S01E02 1080p WEB-DL"),
    ]
    plan = selector_tv.build_plan(requested, _ranked_map(requested, results))
    assert plan["gaps"] == []
    # um torrent só: o pack cobre os três episódios
    assert len(plan["torrents"]) == 1
    assert plan["torrents"][0]["coverage_assigned"] == requested


def test_plan_avulso_melhor_nao_e_sacrificado():
    # E02 tem um BluRay avulso (tier maior): ele entra MESMO já havendo pack
    requested = [(1, 1), (1, 2)]
    results = [
        cand("Breaking Bad S01 1080p WEB-DL", size=9_000_000_000),
        cand("Breaking Bad S01E02 1080p BluRay"),
    ]
    plan = selector_tv.build_plan(requested, _ranked_map(requested, results))
    assert len(plan["torrents"]) == 2
    assert plan["assignments"][(1, 2)]["title"] == "Breaking Bad S01E02 1080p BluRay"
    assert plan["assignments"][(1, 1)]["title"] == "Breaking Bad S01 1080p WEB-DL"


def test_plan_marca_lacunas():
    requested = [(1, 1), (1, 99)]
    results = [cand("Breaking Bad S01E01 1080p WEB-DL")]
    plan = selector_tv.build_plan(requested, _ranked_map(requested, results))
    assert plan["gaps"] == [(1, 99)]
    assert (1, 1) in plan["assignments"]
