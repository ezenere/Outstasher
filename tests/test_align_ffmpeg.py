"""Alinhamento GCC-PHAT REAL (ffmpeg + numpy, sem stub).

Gera um par de arquivos longos onde o segundo é o primeiro atrasado por um
offset conhecido, e verifica que o merger MEDE esse offset e o compensa — testa
o algoritmo de alinhamento de ponta a ponta, não só o encanamento da conversão.
"""
import re
import subprocess

import pytest

from services import merger

pytestmark = pytest.mark.ffmpeg

quiet = lambda *a, **k: None  # noqa: E731

# arquivos longos o bastante para duas janelas de validação do offset
# (merger.alignment_windows(400) = 0:30 e 4:40). Áudio com padrão rico
# (várias frequências) para o GCC-PHAT ter o que casar.
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


# -------------------- validação em janelas espaçadas --------------------
# Arquivos LONGOS (30-60 min) para o plano real de janelas (uma a cada ~20
# min), sem encurtar constante nenhuma. O áudio é gerado em numpy a 8 kHz
# (a taxa da análise) e muxado com um vídeo mínimo: gerar 1 h assim custa
# ~1 s, e o "dublado" sai do mesmo sinal com o corte/atraso/drift exato.

SR = 8000


def _noise(dur, seed, silence=()):
    """Ruído de banda larga (correlaciona) com trechos de silêncio."""
    import numpy as np
    x = np.random.default_rng(seed).standard_normal(int(dur * SR))
    x = (0.25 * np.convolve(x, np.ones(4) / 4, mode="same")).astype(np.float32)
    for a, b in silence:
        x[int(a * SR):int(b * SR)] = 0.0
    return x


def _mkv(path, samples, lang):
    import wave

    import numpy as np
    wav = path.with_suffix(".wav")
    with wave.open(str(wav), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SR)
        wf.writeframes(np.clip(samples * 32767, -32768, 32767).astype("<i2").tobytes())
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", f"color=c=gray:s=32x32:r=1:d={len(samples) / SR:.3f}",
         "-i", str(wav), "-map", "0:v", "-map", "1:a",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
         "-c:a", "flac", "-metadata:s:a:0", f"language={lang}", str(path)],
        check=True)
    wav.unlink()
    return path


def _delay(x, s):
    import numpy as np
    return np.concatenate([np.zeros(int(s * SR), np.float32), x])


def test_offset_constante_em_todas_as_janelas(tmp_path):
    x = _noise(1800, seed=11)
    ref = _mkv(tmp_path / "ref.mkv", x, "eng")
    dub = _mkv(tmp_path / "dub.mkv", _delay(x, 1.25), "por")
    logs = []
    result = merger.merge(str(ref), str(dub), str(tmp_path / "out.mkv"), "pt",
                          log=logs.append, allow_drift=False)
    assert abs(result.offset_ms - 1250) < 1, result.offset_ms
    per_window = [m for m in logs if m.startswith("Offset na janela")]
    assert len(per_window) == 3 and all("+1250.0 ms" in m for m in per_window), per_window
    assert any("consistentes em 3 de 3" in m for m in logs)
    # constante: stream copy com o sync no container, nada re-encodado
    assert not any("re-encodado" in n for n in result.notes)


def test_degrau_depois_da_janela_dos_60_porcento_vira_version_mismatch(tmp_path):
    """O dublado perdeu 400 ms aos 24:10 de um arquivo de 30 min. As duas
    janelas antigas (0:30-5:30 e 18:00-23:00) estavam antes do corte e
    concordavam; a última janela nova (24:30) vê o degrau."""
    import numpy as np
    x = _noise(1800, seed=12)
    cut = 1450.0
    old_start2 = min(max(330.0, 0.6 * 1800), 1800 - 330.0)
    assert old_start2 + 300 < cut
    ref = _mkv(tmp_path / "ref.mkv", x, "eng")
    dub = _mkv(tmp_path / "dub.mkv",
               np.concatenate([x[:int(cut * SR)], x[int((cut + 0.4) * SR):]]), "por")
    with pytest.raises(merger.VersionMismatch) as ei:
        merger.merge(str(ref), str(dub), str(tmp_path / "out.mkv"), "pt",
                     log=quiet, allow_drift=False)
    e = ei.value
    assert e.verdict == "cut" and (e.tau1_ms, e.tau2_ms) == (0.0, -400.0)
    assert [round(o) for _, o, _ in e.windows] == [0, 0, -400]
    assert not (tmp_path / "out.mkv").exists()   # abortou antes do ffmpeg pesado


def test_drift_linear_e_corrigido_na_velocidade(tmp_path):
    """Dublado 0,008% mais lento (288 ms/h): os offsets das 4 janelas caem
    numa reta e o merge corrige a velocidade em vez de parar. Na saída, a
    dublagem fica alinhada com o original do começo ao fim."""
    import numpy as np
    rate = 8e-5
    x = _noise(3600, seed=13)
    idx = np.arange(int(len(x) * (1 + rate))) / (1 + rate)
    slow = np.interp(idx[idx < len(x) - 1], np.arange(len(x)), x).astype(np.float32)
    ref = _mkv(tmp_path / "ref.mkv", x, "eng")
    dub = _mkv(tmp_path / "dub.mkv", slow, "por")
    out = tmp_path / "out.mkv"
    logs = []
    result = merger.merge(str(ref), str(dub), str(out), "pt",
                          log=logs.append, allow_drift=False)
    assert any("drift linear" in m for m in logs), logs
    assert result.input_rates[1] == pytest.approx(1 + rate, rel=2e-6)
    # 288 ms/h reais; a reta pelas 4 janelas acerta a menos de 1%
    assert any(re.match(r"drift linear de \+28\d ms/h corrigido", n)
               for n in result.notes), result.notes
    # saída: a:0 = por (corrigida), a:1 = eng (referência)
    for t in (60.0, 1500.0, 2900.0):
        tau, q = merger._measure_window(str(out), 1, str(out), 0, t)
        assert abs(tau) < 0.005 and q >= merger.ALIGN_MIN_QUALITY, (t, tau, q)


def test_janela_em_silencio_e_ignorada(tmp_path):
    """2,5 min de silêncio nos dois arquivos bem em cima da 2ª janela: o pico
    cai ao nível do ruído, a janela é descartada e as outras três decidem."""
    x = _noise(3000, seed=14, silence=[(800.0, 950.0)])
    ref = _mkv(tmp_path / "ref.mkv", x, "eng")
    dub = _mkv(tmp_path / "dub.mkv", _delay(x, 0.3), "por")
    assert 800 < merger.alignment_windows(3000.0)[1] < 950 - merger.ALIGN_DURATION
    logs = []
    result = merger.merge(str(ref), str(dub), str(tmp_path / "out.mkv"), "pt",
                          log=logs.append, allow_drift=False)
    assert abs(result.offset_ms - 300) < 1, result.offset_ms
    assert any("janela 2/4" in m and "descartada" in m for m in logs), logs
    assert any("consistentes em 3 de 4" in m for m in logs)


# ---- merge forçado (allow_drift=True, o "Continuar" da pausa de drift) ----
# Arquivos curtos e janelas injetadas: o foco é o que o merge FAZ com o
# veredito, não a medição (essa está nos testes acima).

def _fixed_windows(monkeypatch, windows):
    monkeypatch.setattr(merger, "measure_offsets", lambda *a, **k: list(windows))


def test_forcado_com_degrau_usa_o_offset_do_inicio(tmp_path, make_media, monkeypatch):
    _fixed_windows(monkeypatch, [(30.0, 0.0, 80.0), (1200.0, 0.0, 80.0),
                                 (2400.0, 0.5, 80.0), (3600.0, 0.5, 80.0)])
    f1, f2 = make_media(tmp_path / "a.mkv", ["eng"]), make_media(tmp_path / "b.mkv", ["por"])
    result = merger.merge(str(f1), str(f2), str(tmp_path / "out.mkv"), "pt",
                          log=quiet, allow_drift=True)
    assert result.offset_ms == 0.0
    assert any("Offsets divergem entre as janelas (+0 a +500 ms)" in n
               and "offset do início" in n for n in result.notes), result.notes
    assert any("re-encodado" in n for n in result.notes)   # caminho antigo do drift


def test_forcado_sem_confirmacao_mas_concordando_fica_no_container(tmp_path, make_media,
                                                                   monkeypatch):
    # a última janela não correlaciona; as boas concordam em +120 ms
    _fixed_windows(monkeypatch, [(30.0, 0.120, 80.0), (1200.0, 0.121, 80.0),
                                 (2400.0, 0.119, 80.0), (3600.0, 41.0, 7.0)])
    f1, f2 = make_media(tmp_path / "a.mkv", ["eng"]), make_media(tmp_path / "b.mkv", ["por"])
    result = merger.merge(str(f1), str(f2), str(tmp_path / "out.mkv"), "pt",
                          log=quiet, allow_drift=True)
    assert result.offset_ms == 120.0                         # a mediana
    assert result.input_shifts[0] == pytest.approx(0.120)    # -itsoffset, sem re-encode
    assert not any("re-encodado" in n for n in result.notes)
    assert any("Offset não confirmado (a última janela" in n for n in result.notes)
