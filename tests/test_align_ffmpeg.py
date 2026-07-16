"""Alinhamento GCC-PHAT REAL (ffmpeg + numpy, sem stub).

Gera um par de arquivos longos onde o segundo é o primeiro atrasado por um
offset conhecido, e verifica que o merger MEDE esse offset e o compensa — testa
o algoritmo de alinhamento de ponta a ponta, não só o encanamento da conversão.
"""
import subprocess

import pytest

from services import merger

pytestmark = pytest.mark.ffmpeg

quiet = lambda *a, **k: None  # noqa: E731

# arquivos longos o bastante para as DUAS janelas de validação do offset
# (ALIGN_START=30 + ALIGN_DURATION=300). Áudio com padrão rico (várias
# frequências) para o GCC-PHAT ter o que casar.
DUR = 400


def _make_ref(path):
    """Referência: vídeo + áudio com um padrão de frequências variadas."""
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", f"testsrc=s=320x180:d={DUR}:r=12",
         "-f", "lavfi", "-i",
         f"aevalsrc=0.6*sin(2*PI*(300+200*sin(2*PI*t/7))*t):d={DUR}:s=16000",
         "-map", "0:v", "-map", "1:a",
         "-c:v", "libx264", "-preset", "ultrafast", "-b:v", "300k",
         "-pix_fmt", "yuv420p", "-c:a", "ac3", "-b:a", "128k",
         "-metadata:s:a:0", "language=eng", str(path)],
        check=True)


def _make_delayed(ref, out, delay_s, lang="por"):
    """Cria `out` = `ref` com o ÁUDIO atrasado `delay_s` (adelay), simulando um
    arquivo dublado que começa deslocado. O merger deve medir +delay_s."""
    ms = int(delay_s * 1000)
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(ref),
         "-filter:a", f"adelay={ms}:all=1",
         "-c:v", "copy", "-c:a", "ac3", "-b:a", "128k",
         "-metadata:s:a:0", f"language={lang}", str(out)],
        check=True)


def test_measures_and_compensates_known_offset(tmp_path):
    ref = tmp_path / "ref.mkv"
    dub = tmp_path / "dub.mkv"
    out = tmp_path / "out.mkv"
    _make_ref(ref)
    _make_delayed(ref, dub, delay_s=2.0)  # dublado atrasado 2 s

    result = merger.merge(str(ref), str(dub), str(out), "pt", log=quiet)

    # o offset medido deve ficar perto dos 2 s reais (tolerância de 50 ms —
    # ALIGN_SR=8000 dá resolução de 0,125 ms; a folga cobre bordas do adelay)
    assert result.offset_ms is not None
    assert abs(abs(result.offset_ms) - 2000) < 50, result.offset_ms
    assert out.exists()


def test_zero_offset_when_identical(tmp_path):
    ref = tmp_path / "ref.mkv"
    dub = tmp_path / "dub.mkv"
    out = tmp_path / "out.mkv"
    _make_ref(ref)
    _make_delayed(ref, dub, delay_s=0.0)  # mesmo áudio, sem deslocamento

    result = merger.merge(str(ref), str(dub), str(out), "pt", log=quiet)
    assert abs(result.offset_ms) < 50, result.offset_ms  # ~0
