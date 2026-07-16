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
    # hardware: as duas famílias sempre aparecem; disponível só com GPU real
    assert {a["id"] for a in caps["hw_accels"]} == {"nvenc", "qsv"}
    assert vids["vvc"]["hw"] == []  # VVC não tem encoder de hardware
    for c in caps["video_codecs"]:
        for h in c["hw"]:
            assert h["encoder"] in tc.HW_CRF_RANGES
            assert h["ten_bit"] == (h["encoder"] in tc.HW_10BIT)


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


# -------------------- hardware encoding --------------------

def test_hw_presets_and_ranges_complete():
    # todo encoder de HW oferecido tem mapa de preset e faixa de qualidade
    for _label, mapping in tc.HW_FAMILIES.values():
        for enc in mapping.values():
            assert set(tc._PRESETS[enc]) == set(tc.PRESET_LEVELS)
            assert enc in tc.HW_CRF_RANGES


def test_validate_hw_rejects():
    with pytest.raises(ValueError):
        tc.validate({"hw_accel": "cuda"})  # família desconhecida
    with pytest.raises(ValueError):        # VVC não tem encoder de hardware
        tc.validate({"video_codec": "vvc", "hw_accel": "nvenc", "quality_mode": "crf"})
    with pytest.raises(ValueError):        # H.264 em HW é sempre 8-bit
        tc.validate({"video_codec": "h264", "hw_accel": "qsv",
                     "video_bitrate": 4000, "bit_depth": "10"})


def test_hw_plan_or_reject():
    """Com GPU real o plano usa o encoder de HW (args -cq/-global_quality,
    pix_fmt nv12/p010le); sem GPU a validação recusa com mensagem clara.
    Os dois caminhos são o comportamento real do ambiente — nada é falseado."""
    vs = vstream(codec="h264", br=20_000_000)
    for hw, (_label, mapping) in tc.HW_FAMILIES.items():
        enc = mapping["hevc"]
        payload = {"video_codec": "hevc", "hw_accel": hw,
                   "quality_mode": "crf", "crf": 25, "preset": "fast"}
        if tc.hw_encoder_works(enc):
            p = tc.plan_video(probe(vs), vs, tc.validate(payload))
            assert p.encode and p.encoder == enc
            assert "-preset" in p.args and tc._PRESETS[enc]["fast"] in p.args
            if hw == "nvenc":
                assert "-cq" in p.args and "25" in p.args and "-crf" not in p.args
                assert "-rc-lookahead" in p.args  # rate control reforçado
            else:
                assert "-global_quality" in p.args and "-crf" not in p.args
                assert "-extbrc" in p.args and "-look_ahead_depth" in p.args
            assert "-g" in p.args    # GOP longo (default de HW é curto)
            assert "nv12" in p.args  # pix_fmt de HW (fonte 8-bit)
        else:
            with pytest.raises(ValueError, match="não está disponível"):
                tc.validate(payload)


def test_hw_crf_range_scale():
    # AV1 em software vai a 63; em NVENC o -cq para em 51 — a validação segue
    # a escala do encoder escolhido
    assert tc.validate({"video_codec": "av1", "quality_mode": "crf", "crf": 60}).crf == 60
    if tc.hw_encoder_works("av1_nvenc"):
        with pytest.raises(ValueError, match="fora da faixa"):
            tc.validate({"video_codec": "av1", "hw_accel": "nvenc",
                         "quality_mode": "crf", "crf": 60})


def test_describe_hw():
    o = tc.ConvertOptions(video_codec="hevc", hw_accel="nvenc",
                          quality_mode="crf", crf=25)
    assert tc.describe(o)[0] == "HEVC (NVENC)"
    # "manter codec" + downscale em HW: a tag do HW aparece sozinha
    o = tc.ConvertOptions(resolution="1080", hw_accel="qsv",
                          quality_mode="crf", crf=25)
    assert "QSV" in tc.describe(o)
    # sem re-encode de vídeo, hw_accel é inerte
    o = tc.ConvertOptions(audio_codec="aac", hw_accel="nvenc")
    assert tc.describe(o) == ["áudio AAC"]


# -------------------- HDR / sinalização de cor --------------------

def test_plan_video_color_signaling():
    # fonte HDR10: a sinalização de cor é reaplicada explicitamente no encode
    vs = vstream(codec="hevc", w=3840, h=2160, br=40_000_000, pix="yuv420p10le")
    vs.update(color_primaries="bt2020", color_transfer="smpte2084",
              color_space="bt2020nc", color_range="tv")
    p = tc.plan_video(probe(vs), vs, tc.validate(
        {"video_codec": "av1", "quality_mode": "crf", "crf": 22}))
    assert p.encode
    for flag, val in (("-color_primaries", "bt2020"), ("-color_trc", "smpte2084"),
                      ("-colorspace", "bt2020nc"), ("-color_range", "tv")):
        i = p.args.index(flag)
        assert p.args[i + 1] == val, (flag, p.args)
    assert any("mastering display" in n for n in p.notes), p.notes
    # fonte SDR sem cor declarada: nenhuma flag de cor inventada
    vs = vstream(codec="h264", br=20_000_000)
    p = tc.plan_video(probe(vs), vs, tc.validate({"video_codec": "hevc", "video_bitrate": 3000}))
    assert "-color_primaries" not in p.args and "-colorspace" not in p.args


def test_plan_video_dolby_vision_note():
    vs = vstream(codec="hevc", br=40_000_000, pix="yuv420p10le")
    vs["side_data_list"] = [{"side_data_type": "DOVI configuration record"}]
    p = tc.plan_video(probe(vs), vs, tc.validate(
        {"video_codec": "av1", "quality_mode": "crf", "crf": 22}))
    assert any("Dolby Vision" in n for n in p.notes), p.notes


def test_fps_and_frac_helpers():
    assert tc._fps_of({"avg_frame_rate": "24000/1001"}) == pytest.approx(23.976, abs=1e-3)
    assert tc._fps_of({"avg_frame_rate": "0/0"}) == 0.0
    assert tc._frac("34000/50000") == pytest.approx(0.68)
    assert tc._frac("1000") == 1000.0
    assert tc._frac("lixo") is None and tc._frac(None) is None


def test_mkvpropedit_cmd():
    md = {"mastering": {"red_x": 0.68, "red_y": 0.32, "max_luminance": 1000.0,
                        "min_luminance": 0.005, "white_point_x": 0.3127},
          "light": {"max_content": 1000, "max_average": 400}}
    cmd = tc._mkvpropedit_cmd("out.mkv", md)
    assert cmd[:4] == ["mkvpropedit", "out.mkv", "--edit", "track:v1"]
    assert "chromaticity-coordinates-red-x=0.68" in cmd
    assert "max-luminance=1000" in cmd and "min-luminance=0.005" in cmd
    assert "white-coordinates-x=0.3127" in cmd
    assert "max-content-light=1000" in cmd and "max-frame-light=400" in cmd


@pytest.mark.ffmpeg
def test_hdr_metadata_roundtrip(tmp_path):
    """Fonte HEVC 10-bit com HDR10 real (SEI de mastering display + CLL via
    libx265) -> _hdr_side_data lê; após re-encode para H.264 (que descarta o
    SEI), preserve_hdr_metadata reage conforme o ambiente: reinjeta se o
    mkvpropedit existir, senão avisa para instalar o mkvtoolnix."""
    import shutil as _shutil
    import subprocess
    src = str(tmp_path / "hdr.mkv")
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "testsrc=s=320x180:d=1:r=24",
        "-c:v", "libx265", "-preset", "ultrafast", "-pix_fmt", "yuv420p10le",
        "-x265-params",
        "master-display=G(13250,34500)B(7500,3000)R(34000,16000)WP(15635,16450)"
        "L(10000000,1):max-cll=1000,400",
        "-color_primaries", "bt2020", "-color_trc", "smpte2084",
        "-colorspace", "bt2020nc", src], check=True)

    md = tc._hdr_side_data(src)
    assert md and "mastering" in md, md
    assert md["mastering"]["max_luminance"] == pytest.approx(1000.0)
    assert md["light"] == {"max_content": 1000, "max_average": 400}

    out = str(tmp_path / "out.mkv")
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-i", src, "-c:v", "libx264", "-preset", "ultrafast",
                    "-pix_fmt", "yuv420p", "-an", out], check=True)
    notes = tc.preserve_hdr_metadata(src, out)
    assert notes, "fonte HDR10 sem nota de verificação"
    if _shutil.which("mkvpropedit"):
        assert any("reinjetados" in n or "preservados" in n for n in notes), notes
        assert tc._hdr_side_data(out), "metadados não voltaram ao container"
    else:
        assert any("mkvtoolnix" in n for n in notes), notes


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
