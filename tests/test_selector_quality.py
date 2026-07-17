"""Ordenação de torrents estilo Radarr: escada de qualidade (tier) primeiro,
tamanho do arquivo como desempate dentro do tier.

A qualidade é (fonte, resolução) resolvida num tier ordinal: remux 4k >
bluray 4k > webdl 4k > ... descendo até desconhecida. Cuidado especial: um
título que anuncia MAIS DE UMA resolução (1080p E 2160p) tem resolução
desconhecida — não dá para confiar em qual é a do arquivo.
"""
from services import selector


def cand(title, seeders=50, size=8_000_000_000):
    return {"title": title, "seeders": seeders, "size": size,
            "magnet": f"magnet:?xt=urn:btih:{abs(hash(title)):x}", "tracker": "t"}


def _titles(results, mode="video", movie="Filme", year="2026", **kw):
    ranked, _ = selector.rank(results, mode, movie, year, **kw)
    return [r["title"] for r in ranked]


# -------------------- detecção de resolução / fonte --------------------

def test_resolution_of():
    assert selector.resolution_of("Filme 2026 2160p BluRay") == "2160p"
    assert selector.resolution_of("Filme 2026 UHD BluRay") == "2160p"
    assert selector.resolution_of("Filme 2026 1080p WEB-DL") == "1080p"
    assert selector.resolution_of("Filme 2026 720p BDRip") == "720p"
    assert selector.resolution_of("Filme 2026 BluRay") is None  # sem resolução


def test_resolution_multiple_is_unknown():
    # o ponto sensível: dois rótulos distintos -> desconhecida
    assert selector.resolution_of("Filme 2026 UHD BluRay 1080p") is None      # UHD == 2160p + 1080p
    assert selector.resolution_of("Filme 2026 1080p 2160p Remux") is None
    assert selector.resolution_of("Filme 2026 4K 720p") is None
    # o MESMO rótulo repetido não conta como conflito
    assert selector.resolution_of("Filme 2026 2160p UHD BluRay") == "2160p"   # 2160p e UHD são o mesmo


def test_source_of():
    assert selector.source_of("Filme 2026 2160p BluRay REMUX") == "remux"
    assert selector.source_of("Filme 2026 2160p BluRay") == "bluray"
    assert selector.source_of("Filme 2026 2160p BDRip") == "bdrip"
    assert selector.source_of("Filme 2026 2160p WEB-DL") == "webdl"
    assert selector.source_of("Filme 2026 2160p WEBRip") == "webrip"
    assert selector.source_of("Filme 2026 HDTV") == "hdtv"
    assert selector.source_of("Filme 2026 1080p") is None


# -------------------- a escada (tiers) --------------------

def test_quality_ladder_order():
    # a ordem canônica da escada: cada degrau vale mais que o de baixo
    ladder = [
        "Filme 2026 2160p BluRay REMUX",   # 4K remux
        "Filme 2026 2160p BluRay x265",    # 4K bluray
        "Filme 2026 2160p WEB-DL x265",    # 4K webdl
        "Filme 2026 2160p WEBRip x265",    # 4K webrip
        "Filme 2026 2160p BDRip x265",     # 4K bdrip (re-encode do disco: o pior)
        "Filme 2026 1080p BluRay REMUX",   # 1080p remux
        "Filme 2026 1080p BluRay x264",    # 1080p bluray
        "Filme 2026 1080p WEB-DL x264",    # 1080p webdl
        "Filme 2026 720p BluRay x264",     # 720p
        "Filme 2026 HDTV",                 # tv
    ]
    tiers = [selector.quality_tier(t)[0] for t in ladder]
    assert tiers == sorted(tiers, reverse=True), tiers  # estritamente decrescente
    assert len(set(tiers)) == len(tiers), tiers          # sem empates entre degraus


def test_4k_source_order_within_resolution():
    # dentro de 4K: remux > bluray > webdl > bdrip? não — webdl e bdrip empatam
    # em fonte, mas bluray > ambos e remux > todos
    assert (selector.quality_tier("Filme 2026 2160p REMUX")[0]
            > selector.quality_tier("Filme 2026 2160p BluRay")[0]
            > selector.quality_tier("Filme 2026 2160p WEB-DL")[0])


def test_resolution_dominates_source():
    # 4K webdl vence 1080p remux (a escada é por resolução primeiro)
    assert (selector.quality_tier("Filme 2026 2160p WEB-DL")[0]
            > selector.quality_tier("Filme 2026 1080p BluRay REMUX")[0])


def test_cam_is_bottom_and_rejected():
    tier, _ = selector.quality_tier("Filme 2026 HDCAM")
    assert tier <= selector.MIN_TIER
    ranked = _titles([cand("Filme 2026 1080p BluRay"), cand("Filme 2026 HDCAM")])
    assert "Filme 2026 HDCAM" not in ranked  # rejeitado por qualidade


def test_quality_label():
    assert selector.quality_tier("Filme 2026 2160p BluRay REMUX")[1] == "4K Remux"
    assert selector.quality_tier("Filme 2026 1080p WEB-DL")[1] == "1080p WEB-DL"
    assert selector.quality_tier("Filme 2026 UHD BluRay 1080p")[1] == "BluRay"  # res unknown
    assert selector.quality_tier("Filme 2026 sem nada")[1] == "Desconhecida"


# -------------------- upscale de IA (resolução inflada) --------------------

def test_is_ai_upscale_detects_common_phrasings():
    for t in ["Filme 1975 2160p AI Upscaled BluRay",
              "Filme 1975 4K AI Enhanced WEB-DL",
              "Filme 1975 2160p AI-Upscale x265",
              "Filme 1975 UHD Enhanced by AI",
              "Filme 1975 2160p Neural Upscale",
              "Filme 1975 4K AI Remastered"]:
        assert selector.is_ai_upscale(t), t
    # não deve casar coisas legítimas que contêm "ai"/"remaster" sem IA
    for t in ["Filme 1975 2160p BluRay REMUX",
              "Filme 1975 2160p BluRay Remastered",   # remaster real (não-IA)
              "Airplane 1980 1080p BluRay"]:           # "ai" dentro de Airplane
        assert not selector.is_ai_upscale(t), t


def test_ai_upscale_downgrades_below_native_1080p():
    # um 4K "AI upscaled" tem qualidade real de fonte menor: fica abaixo de
    # 1080p NATIVO, apesar do rótulo 2160p
    up4k = selector.quality_tier("Filme 1975 2160p BluRay AI Upscaled")[0]
    native1080 = selector.quality_tier("Filme 1975 1080p BluRay x264")[0]
    native4k = selector.quality_tier("Filme 1975 2160p BluRay x265")[0]
    assert up4k < native1080 < native4k


def test_ai_upscale_label_marks_it():
    tier, label = selector.quality_tier("Filme 1975 2160p BluRay AI Upscaled")
    assert "AI Upscale" in label and label.startswith("4K")  # rótulo real preservado


def test_ai_upscale_does_not_promote_low_res():
    # 480p "enhanced" não pode SUBIR de degrau — o rebaixamento nunca melhora
    plain = selector.quality_tier("Filme 1975 480p DVDRip")[0]
    enhanced = selector.quality_tier("Filme 1975 480p AI Enhanced DVDRip")[0]
    assert enhanced == plain


def test_ai_upscale_4k_sinks_in_ranking():
    def c(t, size):
        return {"title": t, "seeders": 50, "size": size,
                "magnet": f"magnet:{abs(hash(t)):x}", "tracker": "x"}
    # 4K AI upscale enorme não vence 1080p remux nativo
    results = [c("Filme 1975 2160p BluRay AI Upscaled", 40_000_000_000),
               c("Filme 1975 1080p BluRay REMUX", 18_000_000_000)]
    ranked, _ = selector.rank(results, "video", "Filme", "1975")
    assert ranked[0]["title"] == "Filme 1975 1080p BluRay REMUX"


# -------------------- ordenação por tier + tamanho --------------------

def test_tier_beats_size():
    # 4K bluray de 16 GB vence 1080p remux de 24 GB (tier manda sobre tamanho)
    results = [cand("Filme 2026 1080p BluRay REMUX", size=24_000_000_000),
               cand("Filme 2026 2160p BluRay x265", size=16_000_000_000)]
    assert _titles(results)[0] == "Filme 2026 2160p BluRay x265"


def test_size_breaks_tie_within_tier():
    # mesmo tier (4K bluray): o maior arquivo vem primeiro
    results = [cand("Filme 2026 2160p BluRay A", size=4_000_000_000),
               cand("Filme 2026 2160p BluRay B", size=22_000_000_000),
               cand("Filme 2026 2160p BluRay C", size=16_000_000_000)]
    assert _titles(results) == ["Filme 2026 2160p BluRay B",
                                "Filme 2026 2160p BluRay C",
                                "Filme 2026 2160p BluRay A"]


def test_multi_resolution_title_sinks():
    # "UHD BluRay 1080p" anuncia 2160p E 1080p -> resolução desconhecida ->
    # NÃO pode rankear como 4K, mesmo com mais seeders/tamanho
    results = [cand("Filme 2026 UHD BluRay 1080p DD", seeders=999, size=6_000_000_000),
               cand("Filme 2026 2160p BluRay x265", seeders=10, size=5_000_000_000)]
    assert _titles(results)[0] == "Filme 2026 2160p BluRay x265"


def test_unknown_size_sinks_within_tier():
    # tamanho 0/desconhecido cai para o fim do tier
    results = [cand("Filme 2026 2160p BluRay A", size=0),
               cand("Filme 2026 2160p BluRay B", size=10_000_000_000)]
    assert _titles(results)[0] == "Filme 2026 2160p BluRay B"


def test_seeders_dont_override_tier():
    # muitos seeders num 1080p não superam um 4K com poucos (o mecanismo antigo
    # somava seeders ao score e podia inverter isso)
    results = [cand("Filme 2026 1080p WEB-DL", seeders=5000, size=5_000_000_000),
               cand("Filme 2026 2160p WEB-DL", seeders=3, size=15_000_000_000)]
    assert _titles(results)[0] == "Filme 2026 2160p WEB-DL"


# -------------------- interação com modo áudio --------------------

def test_audio_strong_marker_beats_quality(temp_db):
    # dublagem confirmada (marcador forte) vence qualidade de imagem melhor
    results = [cand("Filme 2026 2160p BluRay REMUX", seeders=100),   # sem dublagem
               cand("Filme 2026 1080p WEB-DL Dublado", seeders=100)]  # PT-BR
    ranked = _titles(results, mode="audio", language="pt")
    assert ranked[0] == "Filme 2026 1080p WEB-DL Dublado"


def test_audio_quality_orders_within_dubbed(temp_db):
    # entre dois dublados, a escada de qualidade decide
    results = [cand("Filme 2026 1080p WEB-DL Dublado", seeders=50),
               cand("Filme 2026 2160p BluRay Dublado", seeders=50)]
    ranked = _titles(results, mode="audio", language="pt")
    assert ranked[0] == "Filme 2026 2160p BluRay Dublado"
