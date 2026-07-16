"""Planejador das opções avançadas de conversão (services.transcode).

Testa capacidades, validação e os planos de vídeo/áudio SEM falsear os encoders:
usa o ffmpeg real do ambiente. As asserções sobre codecs específicos exigem
que o ffmpeg tenha os encoders comuns (libx264/libx265/libsvtav1 + aac/ac3/
flac/libopus); se faltar, o teste falha com o motivo — não é mascarado.
"""
import pytest

from services import transcode as tc

# encoders que estes testes assumem existir no ffmpeg do ambiente
_NEEDED = {"libx264", "libx265", "libsvtav1", "aac", "ac3", "flac", "libopus", "libvorbis"}


@pytest.fixture(autouse=True)
def _require_encoders(real_encoders):
    faltando = _NEEDED - real_encoders
    if faltando:
        pytest.fail(
            "O ffmpeg deste ambiente não tem os encoders que estes testes "
            f"assumem: {', '.join(sorted(faltando))}. Instale um ffmpeg com "
            "libx264/libx265/libsvtav1/aac/ac3/flac/libopus/libvorbis.")


# -------------------- helpers de streams sintéticas --------------------

def vstream(codec="h264", w=3840, h=1608, br=None, pix="yuv420p"):
    s = {"codec_type": "video", "codec_name": codec, "width": w, "height": h,
         "pix_fmt": pix, "_type_index": 0}
    if br:
        s["bit_rate"] = str(br)
    return s


def astream(codec="ac3", ch=2, br=None, lang=None, idx=0):
    s = {"codec_type": "audio", "codec_name": codec, "channels": ch, "_type_index": idx}
    if br:
        s["bit_rate"] = str(br)
    if lang:
        s["tags"] = {"language": lang}
    return s


def probe(v, audios=(), fmt_br=None):
    fmt = {"bit_rate": str(fmt_br)} if fmt_br else {}
    return {"streams": [v, *audios], "format": fmt}


# -------------------- capabilities / validate --------------------

def test_capabilities(real_encoders):
    caps = tc.capabilities()
    vids = {c["id"]: c for c in caps["video_codecs"]}
    assert vids["av1"]["available"] and vids["av1"]["encoder"] in real_encoders
    assert vids["h264"]["available"] and vids["hevc"]["available"]
    # VVC só se o ffmpeg tiver libvvenc — reflete o ambiente, não um valor fixo
    assert vids["vvc"]["available"] == ("libvvenc" in real_encoders)
    auds = {c["id"]: c for c in caps["audio_codecs"]}
    assert auds["aac"]["max_channels"] == 2
    assert auds["flac"]["lossless"] and auds["flac"]["default_kbps"] is None
    assert caps["video_bitrate_kbps"] == [100, 150000]


def test_validate_ok():
    o = tc.validate({"video_codec": "hevc", "video_bitrate": 4000})
    assert o.video_codec == "hevc" and o.video_bitrate == 4000
    assert not tc.validate({}).wants_video_encode()  # tudo default = clássico
    o = tc.validate({"video_codec": "av1", "quality_mode": "crf"})
    assert o.crf is None  # o default do CRF é resolvido no plano, não aqui


@pytest.mark.parametrize("bad", [
    {"video_codec": "h264"},                              # falta bitrate
    {"video_codec": "h264", "video_bitrate": 999_999},    # fora da faixa
    {"video_codec": "h264", "video_bitrate": 50},         # abaixo do mínimo
    {"resolution": "999"},
    {"audio_codec": "mp3"},                               # não oferecido
    {"video_codec": "h264", "quality_mode": "crf", "crf": 99},
    {"audio_codec": "opus", "audio_bitrate": 4000},       # bitrate de áudio fora
    {"subtitles": "banana"},
])
def test_validate_rejects(bad):
    with pytest.raises(ValueError):
        tc.validate(bad)


def test_validate_rejects_codec_without_encoder(real_encoders):
    # VVC costuma faltar (sem libvvenc): pedir sem o encoder deve ser recusado
    if "libvvenc" in real_encoders:
        pytest.skip("este ffmpeg TEM libvvenc; não há codec de vídeo ausente para testar")
    with pytest.raises(ValueError):
        tc.validate({"video_codec": "vvc", "video_bitrate": 4000})


# -------------------- plan_video --------------------

def test_plan_video_default_no_encode():
    vs = vstream(br=20_000_000)
    assert not tc.plan_video(probe(vs), vs, tc.validate({})).encode


def test_plan_video_bitrate_up_keeps_source():
    vs = vstream(codec="h264", br=5_000_000)
    p = tc.plan_video(probe(vs), vs, tc.validate({"video_codec": "hevc", "video_bitrate": 8000}))
    assert not p.encode and any("mantido" in n for n in p.notes), p.notes


def test_plan_video_bitrate_down_encodes():
    vs = vstream(codec="h264", br=5_000_000)
    p = tc.plan_video(probe(vs), vs, tc.validate(
        {"video_codec": "hevc", "video_bitrate": 3000, "preset": "slow"}))
    assert p.encode and p.encoder == "libx265"
    assert p.args[:2] == ["-c:v", "libx265"] and "-preset" in p.args and "slow" in p.args
    assert "-b:v" in p.args and "3000k" in p.args
    assert "yuv420p" in p.args  # fonte 8-bit


def test_plan_video_resolution_tolerance_and_downscale():
    # DCI 4K (4096 de largura) "já é" 4K -> sem downscale
    vs = vstream(codec="h264", w=4096, h=2160, br=30_000_000)
    p = tc.plan_video(probe(vs), vs, tc.validate({"resolution": "2160", "quality_mode": "crf"}))
    assert not p.encode and any("mantida" in n for n in p.notes), p.notes
    # 3840 de largura -> reduzir para 1080p
    vs = vstream(codec="h264", w=3840, h=1608, br=30_000_000)
    p = tc.plan_video(probe(vs), vs, tc.validate({"resolution": "1080", "quality_mode": "crf", "crf": 22}))
    assert p.encode and "-vf" in p.args and "scale=1920:-2" in p.args
    assert p.encoder == "libx264"  # "manter codec" re-encoda na mesma família


def test_plan_video_downscale_bitrate_capped():
    vs = vstream(codec="h264", w=3840, h=1608, br=20_000_000)
    p = tc.plan_video(probe(vs), vs, tc.validate({"resolution": "1080", "video_bitrate": 10000}))
    # teto ~ 20M * (1920/3840)^2 = 5M
    assert p.encode and "5000k" in p.args, p.args
    assert any("rebaixado" in n for n in p.notes), p.notes


def test_plan_video_unknown_source_codec_falls_back():
    # vp9 não tem encoder no nosso mapa -> fallback para o melhor disponível
    vs = vstream(codec="vp9", w=3840, h=2160, br=10_000_000)
    p = tc.plan_video(probe(vs), vs, tc.validate({"resolution": "1080", "quality_mode": "crf"}))
    assert p.encode and p.encoder in ("libx265", "libx264", "libsvtav1")
    assert any("sem encoder" in n for n in p.notes), p.notes


def test_plan_video_bit_depth():
    vs10 = vstream(codec="hevc", br=20_000_000, pix="yuv420p10le")
    # H.264 cai para 8-bit por padrão (High10 quase não decodifica)
    p = tc.plan_video(probe(vs10), vs10, tc.validate({"video_codec": "h264", "video_bitrate": 4000}))
    assert "yuv420p" in p.args and "yuv420p10le" not in p.args
    assert any("High10" in n for n in p.notes)
    # forçado explicitamente -> 10-bit
    p = tc.plan_video(probe(vs10), vs10, tc.validate(
        {"video_codec": "h264", "video_bitrate": 4000, "bit_depth": "10"}))
    assert "yuv420p10le" in p.args
    # fonte 8-bit + pedir 10-bit em HEVC -> sobe para 10-bit (menos banding)
    vs8 = vstream(codec="h264", br=20_000_000, pix="yuv420p")
    p = tc.plan_video(probe(vs8), vs8, tc.validate(
        {"video_codec": "hevc", "video_bitrate": 4000, "bit_depth": "10"}))
    assert "yuv420p10le" in p.args and any("banding" in n for n in p.notes)


def test_plan_video_crf_svtav1():
    vs = vstream(codec="h264", br=20_000_000)
    p = tc.plan_video(probe(vs), vs, tc.validate({"video_codec": "av1", "quality_mode": "crf"}))
    assert p.encoder == "libsvtav1" and "-crf" in p.args and "30" in p.args  # default av1
    assert "-preset" in p.args and "6" in p.args


def test_estimate_video_bitrate():
    vs = vstream(codec="h264")  # sem bit_rate no stream
    pr = probe(vs, audios=(astream(br=640_000),), fmt_br=8_000_000)
    assert tc._estimate_video_bitrate(pr, vs) == 8_000_000 - 640_000
    assert tc._estimate_video_bitrate(probe(vs), vs) == 0  # nada declarado
    # sem teto conhecido: converte assim mesmo, avisando
    p = tc.plan_video(probe(vs), vs, tc.validate({"video_codec": "hevc", "video_bitrate": 90000}))
    assert p.encode and any("desconhecido" in n for n in p.notes)


# -------------------- plan_audio --------------------

def test_plan_audio_keep():
    assert not tc.plan_audio(astream(codec="truehd", ch=8), tc.validate({})).encode


def test_plan_audio_downmix_keep_codec():
    s = astream(codec="truehd", ch=8)
    p = tc.plan_audio(s, tc.validate({"channels": "stereo"}))
    assert p.encode and p.encoder == "aac" and p.bitrate_k == 192 and p.out_channels == 2
    p = tc.plan_audio(s, tc.validate({"channels": "surround51"}))
    assert p.encode and p.encoder == "ac3" and p.out_channels == 6


def test_plan_audio_aac_forces_stereo():
    p = tc.plan_audio(astream(codec="ac3", ch=6, br=640_000), tc.validate({"audio_codec": "aac"}))
    assert p.encode and p.encoder == "aac" and p.out_channels == 2
    assert any("limitação" in n for n in p.notes), p.notes


def test_plan_audio_bitrate_rule():
    s = astream(codec="ac3", ch=2, br=384_000)
    p = tc.plan_audio(s, tc.validate({"audio_codec": "opus", "audio_bitrate": 448}))
    assert not p.encode and any("mantida" in n for n in p.notes), p.notes
    p = tc.plan_audio(s, tc.validate({"audio_codec": "opus", "audio_bitrate": 128}))
    assert p.encode and p.encoder == "libopus" and p.bitrate_k == 128


def test_plan_audio_flac():
    # fonte lossy -> mantém (converter para FLAC só infla)
    p = tc.plan_audio(astream(codec="aac", ch=2, br=128_000), tc.validate({"audio_codec": "flac"}))
    assert not p.encode and any("aumentaria" in n for n in p.notes)
    # fonte lossless -> converte, sem -b:a
    p = tc.plan_audio(astream(codec="truehd", ch=6), tc.validate({"audio_codec": "flac"}))
    assert p.encode and p.encoder == "flac" and p.bitrate_k is None
    # DTS-HD MA é lossless; DTS core não
    dts_ma = astream(codec="dts", ch=6)
    dts_ma["profile"] = "DTS-HD MA"
    assert tc._is_lossless_source(dts_ma)
    assert not tc._is_lossless_source(astream(codec="dts", ch=6))


def test_plan_audio_opus_layout_fix():
    p = tc.plan_audio(astream(codec="truehd", ch=6), tc.validate({"audio_codec": "opus"}))
    assert p.encode and p.needs_layout_fix
    args = tc.audio_output_args(1, p)
    assert args[:2] == ["-c:a:1", "libopus"] and "-b:a:1" in args
    assert "-filter:a:1" in args and tc.OPUS_LAYOUT_FIX in args
    # via filter_complex: o aformat vai dentro da chain, não como -filter:a
    assert "-filter:a:1" not in tc.audio_output_args(1, p, via_filter_complex=True)


def test_plan_audio_same_codec_unknown_bitrate_keeps():
    p = tc.plan_audio(astream(codec="opus", ch=2), tc.validate({"audio_codec": "opus"}))
    assert not p.encode


def test_allowed_langs():
    keep = tc.allowed_langs("por", "ja")
    assert {"por", "jpn", "ja", "und", "unknown", ""} <= keep and "eng" not in keep
    keep = tc.allowed_langs("por", "en")
    assert "eng" in keep and "por" in keep
    keep = tc.allowed_langs("spa", None)  # sem idioma original: só alvo + desconhecidos
    assert "spa" in keep and "eng" not in keep


def test_describe():
    o = tc.validate({"video_codec": "hevc", "quality_mode": "crf", "crf": 24,
                     "resolution": "1080", "audio_codec": "opus",
                     "audio_tracks": "target", "subtitles": "none"})
    assert tc.describe(o) == ["HEVC", "1080p", "CRF 24", "áudio OPUS", "só orig+dub", "sem legendas"]
    assert tc.describe(None) == [] and tc.describe(tc.validate({})) == []


def test_describe_preset():
    # preset aparece quando há re-encode de vídeo e ≠ default
    o = tc.validate({"video_codec": "hevc", "video_bitrate": 3000, "preset": "slow"})
    assert tc.describe(o) == ["HEVC", "3.0 Mbps", "preset lento"]
    # preset default não aparece, mesmo com re-encode
    o = tc.validate({"video_codec": "hevc", "video_bitrate": 3000, "preset": "default"})
    assert "preset lento" not in tc.describe(o) and not any("preset" in x for x in tc.describe(o))
    # sem re-encode de vídeo (só áudio): preset é inerte, não aparece
    o = tc.validate({"audio_codec": "aac", "preset": "veryslow"})
    assert tc.describe(o) == ["áudio AAC"]
    assert tc.describe({"video_codec": "av1", "video_bitrate": 3000}) == ["AV1", "3.0 Mbps"]
