"""Merge por segmentos (merger_segments): detecção, validação cruzada, offsets,
montagem. Lógica pura — ffprobe/ffmpeg/medição de offset são mockados."""
from pathlib import Path

import pytest

from services import merger, merger_segments as mseg
from services.merger import MergeResult


# -------------------- parsers --------------------

def test_parse_blackdetect():
    err = """
[blackdetect @ 0x1] black_start:601.2 black_end:602.0 black_duration:0.8
frame= 1000 fps=250
[blackdetect @ 0x1] black_start:1200.5 black_end:1201.1 black_duration:0.6
"""
    assert mseg.parse_blackdetect(err) == [(601.2, 602.0), (1200.5, 1201.1)]


def test_parse_silencedetect():
    err = """
[silencedetect @ 0x2] silence_start: 601.0
[silencedetect @ 0x2] silence_end: 602.3 | silence_duration: 1.3
[silencedetect @ 0x2] silence_start: -0.02
[silencedetect @ 0x2] silence_end: 1.5 | silence_duration: 1.52
[silencedetect @ 0x2] silence_start: 5390.0
"""
    # start<0 vira 0; um silence_start sem end fecha na duração do arquivo
    assert mseg.parse_silencedetect(err, 5400.0) == [(601.0, 602.3), (0.0, 1.5), (5390.0, 5400.0)]


# -------------------- validação cruzada / segmentação --------------------

def test_cross_validate_tolerance():
    blacks = [(601.2, 602.0), (1200.5, 1201.1), (3000.0, 3000.6)]
    silences = [(601.0, 602.3), (1201.5, 1202.0)]  # 2º silêncio a 0.4s do 2º preto
    cuts = mseg.cross_validate(blacks, silences, tolerance=0.5)
    assert len(cuts) == 2 and abs(cuts[0] - 601.6) < 0.01  # corte no meio do preto
    assert len(mseg.cross_validate(blacks, silences, tolerance=0.1)) == 1  # tolerância menor derruba o 2º


def test_filter_cuts_and_build_segments():
    cuts = mseg.filter_cuts([10.0, 100.0, 115.0, 5395.0], duration=5400.0, min_segment=30.0)
    assert cuts == [100.0]  # perto do início/fim/um do outro cai fora
    assert mseg.build_segments(cuts, 5400.0) == [(0.0, 100.0), (100.0, 5400.0)]


# -------------------- offsets por segmento --------------------

def test_offsets_inherit_neighbor(monkeypatch):
    measured = []

    def fake_measure(rp, ra, op, oa, start, dur):
        measured.append((round(start, 1), round(dur, 1)))
        return -0.5 if start < 600 else 2.0
    monkeypatch.setattr(mseg, "_measure_window", fake_measure)

    p = mseg.SegmentParams()
    segs = [(0.0, 600.0), (600.0, 606.0), (606.0, 1200.0)]  # o do meio (6s) é imensurável
    offs = mseg.measure_segment_offsets("r", 0, "o", 0, segs, p, log=lambda m: None)
    assert offs == [-0.5, -0.5, 2.0]  # o curto herdou o vizinho anterior
    assert len(measured) == 2 and measured[0][1] == p.seg_align_dur  # janela com teto


def test_offsets_window_past_dubbed_end_inherits(monkeypatch):
    # dublado mais curto que a referência: a janela cairia além do fim (era o
    # bug "WAV vazio: .../oth.wav") — herda o vizinho em vez de falhar
    measured = []
    monkeypatch.setattr(mseg, "_measure_window",
                        lambda rp, ra, op, oa, start, dur: measured.append((start, dur)) or 1.0)
    p = mseg.SegmentParams()
    segs = [(0.0, 1200.0), (1200.0, 2400.0), (2400.0, 2640.0)]  # ref ~44 min
    offs = mseg.measure_segment_offsets("r", 0, "o", 0, segs, p, log=lambda m: None,
                                        oth_duration=2350.0)  # dublado ~39 min
    assert offs[2] == offs[1]                      # último herdou (janela cairia em ~2465s)
    assert all(st < 2350.0 for st, _ in measured)  # nenhuma extração além do fim

    measured.clear()
    mseg.measure_segment_offsets("r", 0, "o", 0, [(2200.0, 2400.0)], p,
                                 log=lambda m: None, oth_duration=2350.0)
    st, du = measured[0]
    assert st + du <= 2350.0 + 0.1  # janela parcial encolhida para não passar do fim


def test_offsets_failed_measurement_inherits(monkeypatch):
    def flaky(rp, ra, op, oa, start, dur):
        if start > 600:
            raise mseg.MergeError("WAV vazio: /tmp/x/oth.wav")
        return -0.25
    monkeypatch.setattr(mseg, "_measure_window", flaky)
    p = mseg.SegmentParams()
    offs = mseg.measure_segment_offsets("r", 0, "o", 0,
                                        [(0.0, 600.0), (600.0, 1200.0)], p, log=lambda m: None)
    assert offs == [-0.25, -0.25]

    # se NENHUM segmento mede, o erro sobe (não há o que herdar)
    monkeypatch.setattr(mseg, "_measure_window",
                        lambda *a: (_ for _ in ()).throw(mseg.MergeError("WAV vazio: x")))
    with pytest.raises(mseg.MergeError, match="nenhum segmento"):
        mseg.measure_segment_offsets("r", 0, "o", 0, [(0.0, 600.0)], p, log=lambda m: None)


def test_build_segment_chain():
    chain = mseg.build_segment_chain("[1:a:0]", [(0.0, 100.0), (100.0, 250.0)],
                                     [-2.0, 3.5], "a_seg_0")
    assert "asplit=2" in chain
    assert "atrim=start=0.000000:end=98.000000" in chain  # 0-2=-2 clampa em 0; 100-2=98
    assert "adelay=2000:all=1" in chain                   # 2s de silêncio na frente
    assert "atrim=start=103.500000:end=253.500000" in chain
    assert "atrim=end=100.000000" in chain and "atrim=end=150.000000" in chain  # duração exata
    assert "concat=n=2:v=0:a=1[a_seg_0]" in chain


# -------------------- fluxo completo (mockado) --------------------

def _probe(width, lang, channels, dur=5400.0):
    return {"format": {"duration": str(dur)},
            "streams": [
                {"codec_type": "video", "codec_name": "h264", "width": width,
                 "height": int(width * 9 / 16), "pix_fmt": "yuv420p", "disposition": {}},
                {"codec_type": "audio", "codec_name": "aac", "channels": channels,
                 "sample_rate": "48000", "tags": {"language": lang}, "disposition": {}},
            ], "chapters": []}


@pytest.fixture
def seg_env(tmp_path, monkeypatch):
    f1, f2 = tmp_path / "orig.mkv", tmp_path / "dub.mkv"
    f1.write_bytes(b"x")
    f2.write_bytes(b"y")
    monkeypatch.setattr(merger, "_check_tools", lambda: None)
    monkeypatch.setattr(merger, "ffprobe_json",
                        lambda path: _probe(1920, "eng", 6) if "orig" in path else _probe(1280, "und", 2))
    monkeypatch.setattr(mseg, "detect_black", lambda path, v, p: [(1800.0, 1800.8), (3600.0, 3600.6)])
    monkeypatch.setattr(mseg, "detect_silence", lambda path, a, d, p: [(1799.9, 1801.0), (3599.8, 3601.0)])
    return f1, f2, tmp_path


def test_consistent_offsets_delegate_to_classic(seg_env, monkeypatch):
    f1, f2, tmp_path = seg_env
    classic = {}

    def fake_classic(fa, fb, out, target_lang=None, file2_is_target_dub=True,
                     log=print, on_progress=None, allow_drift=True, **kw):
        classic.update(files=(fa, fb), allow_drift=allow_drift)
        return MergeResult(output=out, offset_ms=200.0)
    monkeypatch.setattr(merger, "merge", fake_classic)
    monkeypatch.setattr(mseg, "_measure_window", lambda *a: 0.2)

    res = mseg.merge_segmented(str(f1), str(f2), str(tmp_path / "out.mkv"),
                               target_lang="pt", log=lambda m: None)
    assert classic["files"] == (str(f1), str(f2)) and classic["allow_drift"] is True
    assert any("offsets consistentes" in n for n in res.notes), res.notes


def test_divergent_offsets_remount_per_segment(seg_env, monkeypatch):
    f1, f2, tmp_path = seg_env
    captured = {}
    monkeypatch.setattr(merger, "_run_ffmpeg_progress",
                        lambda cmd, dur, cb: captured.update(cmd=cmd, dur=dur))
    monkeypatch.setattr(mseg, "_measure_window",
                        lambda rp, ra, op, oa, start, dur:
                        -0.5 if start < 1800 else (1.5 if start < 3600 else 3.0))

    res = mseg.merge_segmented(str(f1), str(f2), str(tmp_path / "out2.mkv"),
                               target_lang="pt", log=lambda m: None)
    cmd = " ".join(captured["cmd"])
    assert "asplit=3" in cmd and "concat=n=3:v=0:a=1[a_seg_0]" in cmd
    assert "-map [a_seg_0]" in cmd            # dublado remontado
    assert "-c:a:0 aac" in cmd                # 2ch -> aac
    assert "-map 0:a:0" in cmd                # eng da referência em copy
    assert "-disposition:a:0 default" in cmd  # por (alvo) como default
    assert "language=por" in cmd
    assert captured["dur"] == 5400.0
    assert res.offset_ms == -500.0            # offset do 1º segmento
    assert any("remontado" in n for n in res.notes), res.notes


def test_single_detector_and_both_disabled(seg_env, monkeypatch):
    f1, f2, tmp_path = seg_env
    monkeypatch.setattr(merger, "_run_ffmpeg_progress", lambda cmd, dur, cb: None)
    monkeypatch.setattr(mseg, "_measure_window",
                        lambda rp, ra, op, oa, start, dur:
                        -0.5 if start < 1800 else (1.5 if start < 3600 else 3.0))
    # black desligado: cortes só pelo silêncio (detect_black nem deve ser chamado)
    monkeypatch.setattr(mseg, "detect_black",
                        lambda *a: (_ for _ in ()).throw(AssertionError("black desligado!")))
    res = mseg.merge_segmented(str(f1), str(f2), str(tmp_path / "out3.mkv"), target_lang="pt",
                               params=mseg.SegmentParams(use_black=False), log=lambda m: None)
    assert any("remontado" in n for n in res.notes)
    # os dois detectores desligados: erro
    with pytest.raises(mseg.MergeError, match="pelo menos um detector"):
        mseg.merge_segmented(str(f1), str(f2), str(tmp_path / "out4.mkv"),
                             params=mseg.SegmentParams(use_black=False, use_silence=False))
