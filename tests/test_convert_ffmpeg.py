"""E2E de conversão com ffmpeg REAL: convert_single e merger.merge com opções.

Marcados com @pytest.mark.ffmpeg. O ffmpeg roda de verdade (gera e converte
mídia). O FOCO aqui é o pipeline de conversão (codec/resolução/áudios), então
os testes de merge fixam o offset em 0 (fixture fixed_offset) — o alinhamento
GCC-PHAT real, com arquivos longos, é exercitado em test_align_ffmpeg.py.
"""
from pathlib import Path

import pytest

from services import merger
from services import transcode as tc

pytestmark = pytest.mark.ffmpeg

quiet = lambda *a, **k: None  # noqa: E731


def _langs(streams):
    return sorted((s.get("tags") or {}).get("language") for s in streams)


@pytest.fixture
def fixed_offset(monkeypatch):
    """Fixa o offset em 0 para os testes cujo FOCO é o pipeline de conversão,
    não o alinhamento. Arquivos curtos (6 s) não têm janela de 30 s+ para o
    GCC-PHAT medir — o alinhamento real é exercitado em test_align_ffmpeg.py."""
    monkeypatch.setattr(merger, "_measure_offset", lambda *a, **k: 0.0)


def test_sigkill_reports_oom(tmp_path, make_media):
    """ffmpeg morto por SIGKILL (código -9) — o que o OOM killer faz — vira uma
    MergeError que EXPLICA o OOM e ainda mostra a saída do ffmpeg. Aqui matamos
    o processo à mão via on_start para simular a morte externa."""
    import threading

    src = tmp_path / "in.mkv"
    make_media(src, ["eng"], dur=30, w=1280, h=536)  # longo para dar tempo de matar
    out = tmp_path / "out.mkv"

    def kill_soon(proc):
        # mata o ffmpeg com SIGKILL logo após ele começar (== OOM killer)
        threading.Timer(1.0, proc.kill).start()

    opts = tc.validate({"video_codec": "hevc", "quality_mode": "crf", "crf": 30,
                        "preset": "veryslow"})  # lento, garante que ainda roda quando matamos
    with pytest.raises(merger.MergeError) as ei:
        tc.convert_single(str(src), str(out), opts, log=quiet, on_start=kill_soon)
    msg = str(ei.value)
    assert "OOM" in msg or "memória" in msg or "SIGKILL" in msg, msg
    assert "-9" in msg  # o código bruto continua visível


def test_convert_single_hevc_downscale_target_aac(tmp_path, make_media, ffprobe_streams):
    src = tmp_path / "single.mkv"
    make_media(src, ["eng", "por", "fra"], w=1920, h=800)
    out = tmp_path / "out.mkv"
    opts = tc.validate({"video_codec": "hevc", "quality_mode": "crf", "crf": 35,
                        "preset": "veryfast", "resolution": "480",
                        "audio_tracks": "target", "audio_codec": "aac", "audio_bitrate": 96})
    res = tc.convert_single(str(src), str(out), opts,
                            target_lang="pt", original_lang="en", log=quiet)
    assert not res.linked
    v = ffprobe_streams(out, "video")[0]
    assert v["codec_name"] == "hevc" and v["width"] == 854
    auds = ffprobe_streams(out, "audio")
    assert _langs(auds) == ["eng", "por"]  # fra descartado; eng (original) e por (dub) ficam
    assert all(s["codec_name"] == "aac" for s in auds)


def test_convert_single_noop_hardlinks(tmp_path, make_media):
    src = tmp_path / "single.mkv"
    make_media(src, ["eng", "por"])
    out = tmp_path / "out.mkv"
    res = tc.convert_single(str(src), str(out), tc.validate({}),
                            target_lang="pt", original_lang="en", log=quiet)
    assert res.linked and Path(res.output).exists()
    assert Path(res.output).stat().st_size == src.stat().st_size


def test_convert_single_bitrate_up_hardlinks(tmp_path, make_media):
    src = tmp_path / "single.mkv"
    make_media(src, ["eng", "por"])
    out = tmp_path / "out.mkv"
    res = tc.convert_single(str(src), str(out),
                            tc.validate({"video_codec": "h264", "video_bitrate": 5000}), log=quiet)
    assert res.linked, res.notes
    assert any("mantido" in n for n in res.notes), res.notes


def test_merge_reencodes_audio_to_opus(tmp_path, make_media, ffprobe_streams, fixed_offset):
    f1, f2 = tmp_path / "orig.mkv", tmp_path / "dub.mkv"
    make_media(f1, ["eng"], w=1280, h=536)
    make_media(f2, ["por"], w=640, h=272)
    out = tmp_path / "out.mkv"
    opts = tc.validate({"audio_codec": "opus", "audio_bitrate": 64, "subtitles": "none"})
    merger.merge(str(f1), str(f2), str(out), "pt", log=quiet, convert=opts, original_lang="en")
    v = ffprobe_streams(out, "video")[0]
    assert v["codec_name"] == "h264" and v["width"] == 1280  # vídeo intocado (copy)
    auds = ffprobe_streams(out, "audio")
    assert _langs(auds) == ["eng", "por"]
    assert all(s["codec_name"] == "opus" for s in auds), [s["codec_name"] for s in auds]


def test_merge_with_downscale(tmp_path, make_media, ffprobe_streams, fixed_offset):
    f1, f2 = tmp_path / "orig.mkv", tmp_path / "dub.mkv"
    make_media(f1, ["eng"], w=1280, h=536)
    make_media(f2, ["por"], w=640, h=272)
    out = tmp_path / "out.mkv"
    opts = tc.validate({"resolution": "480", "quality_mode": "crf", "crf": 35})
    merger.merge(str(f1), str(f2), str(out), "pt", log=quiet, convert=opts, original_lang="en")
    v = ffprobe_streams(out, "video")[0]
    assert v["codec_name"] == "h264" and v["width"] == 854
    auds = ffprobe_streams(out, "audio")
    assert all(s["codec_name"] == "ac3" for s in auds)  # áudio intocado (copy)


def test_merge_classic_untouched(tmp_path, make_media, ffprobe_streams, fixed_offset):
    f1, f2 = tmp_path / "orig.mkv", tmp_path / "dub.mkv"
    make_media(f1, ["eng"], w=1280, h=536)
    make_media(f2, ["por"], w=640, h=272)
    out = tmp_path / "out.mkv"
    merger.merge(str(f1), str(f2), str(out), "pt", log=quiet)  # sem convert
    v = ffprobe_streams(out, "video")[0]
    assert v["codec_name"] == "h264" and v["width"] == 1280
    auds = ffprobe_streams(out, "audio")
    assert all(s["codec_name"] == "ac3" for s in auds)
    assert _langs(auds) == ["eng", "por"]
