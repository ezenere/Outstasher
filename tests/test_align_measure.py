"""Medições do refino por áudio das séries (refine._measure e
scan_constant_offset): o que entra no GCC-PHAT e quando o pico VALE.

1. Extração da janela. Com o alvo num keyframe do MKV, o -ss de entrada
   sozinho pulava o pacote de áudio que cobre o instante e a janela saía
   adiantada (AAC a 48 kHz: 21-42 ms; PCM: até 62 ms). Acima do
   CHANGE_TOL_S (30 ms) do refino isso é degrau falso — o match era
   dividido por uma "edição no áudio" que não existe — ou o offset inteiro
   do match saía torto. Agora a extração é a do merge de filmes (pré-roll
   de 1 s + -ss de saída), sem -ss de entrada quando o alvo está no
   primeiro segundo.
2. Piso do ruído. O pico de dois sinais independentes depende do raio de
   busca: ±0,6 s dá 5-7,5; ±60 s (scan) dá 8-12. O limiar antigo (6)
   deixava TODA janela de música/silêncio votar no scan.
"""
import subprocess
import wave

import numpy as np
import pytest

from services import merger
from services.series.align import refine
from services.series.align.classify import Segment

SR = merger.ALIGN_SR


def _noise(dur, seed=None, rng=None):
    """Ruído de banda larga (o _noise de test_align_ffmpeg)."""
    rng = rng if rng is not None else np.random.default_rng(seed)
    x = rng.standard_normal(int(dur * SR))
    return (0.25 * np.convolve(x, np.ones(4) / 4, mode="same")).astype(np.float32)


def _delay(x, s):
    return np.concatenate([np.zeros(int(round(s * SR)), np.float32), x])


def _wav(path, samples):
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SR)
        wf.writeframes(np.clip(samples * 32767, -32768, 32767).astype("<i2").tobytes())
    return path


def _mkv(path, samples, codec, gop=48):
    """MKV como release de streaming: vídeo com GOP fixo (48 quadros a 24 fps
    = keyframe a cada 2 s) e o áudio em `codec` a 48 kHz estéreo."""
    wav = _wav(path.with_suffix(".src.wav"), samples)
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", f"color=c=gray:s=32x32:r=24:d={len(samples) / SR:.3f}",
         "-i", str(wav), "-map", "0:v", "-map", "1:a",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
         "-g", str(gop), "-keyint_min", str(gop), "-sc_threshold", "0",
         "-c:a", codec, "-ar", "48000", "-ac", "2", str(path)], check=True)
    wav.unlink()
    return path


def _keyframes(path):
    p = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                        "-skip_frame", "nokey", "-show_entries", "frame=pts_time",
                        "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True, check=True)
    return [float(v) for v in p.stdout.replace(",", " ").split()]


# -------------------- 1. extração em keyframe --------------------

@pytest.mark.ffmpeg
@pytest.mark.parametrize("codec", ["aac", "pcm_s16le"])
def test_janela_em_keyframe_mede_o_offset_sem_erro(tmp_path, codec):
    """Dublado = original atrasado 0,25 s, os dois com keyframe a cada 2 s.
    Com o palpite já no offset certo, a medição numa janela que começa
    exatamente num keyframe (do dublado ou do original) tem de dar ~0."""
    d = 0.25
    x = _noise(70, seed=41)
    orig = _mkv(tmp_path / "orig.mkv", x, codec)
    dub = _mkv(tmp_path / "dub.mkv", _delay(x, d), codec)
    kfs = [k for k in _keyframes(dub) if 4.0 <= k <= 56.0]
    assert len(kfs) == 27 and all(abs(k / 2 - round(k / 2)) < 1e-3 for k in kfs), kfs
    erros = []
    for k in kfs[::2]:
        # o conteúdo do dublado em `a` é o do original em `a - d`
        for a0 in (k, k + d):     # keyframe no dublado; keyframe no original
            tau, q = refine._measure(str(dub), 0, str(orig), 0, a0, a0 - d, refine.WIN_S)
            assert q >= refine.MIN_PEAK_QUALITY, (a0, q)
            erros.append(round(tau * 1000, 2))
    # sobra só a resolução da análise (0,125 ms por amostra a 8 kHz); antes:
    # 21-42 ms em TODA janela de AAC no keyframe, 0-62 ms em PCM
    assert max(abs(e) for e in erros) < 2.0, erros


@pytest.mark.ffmpeg
@pytest.mark.parametrize("codec", ["aac", "pcm_s16le"])
def test_offset_constante_em_mkv_nao_ganha_corte_nem_desvio(tmp_path, codec):
    """O refino de um match de 5 min com offset CONSTANTE (o dublado 330 ms
    atrasado; o vídeo palpita -0,25 na grade de 0,25 s) em MKV com keyframe a
    cada 1 s — toda janela do perfil começa num keyframe do dublado. Antes, o
    erro da extração (AAC: 21-42 ms em toda janela; PCM: 0-62 ms, variando)
    entortava o offset do match inteiro (medido: 30 ms com AAC, 46 ms com
    PCM — fora do lip sync) e, com o erro variando de janela para janela,
    podia abrir um corte por "edição no áudio" que não existe."""
    d = 0.33
    x = _noise(320, seed=42)
    orig = _mkv(tmp_path / "orig.mkv", x, codec, gop=24)
    dub = _mkv(tmp_path / "dub.mkv", _delay(x, d), codec, gop=24)
    seg = Segment("match", 1.0, 301.0, 0.75, 300.75, offset=-0.25, slope=1.0)
    logs = []
    out = refine.refine_offsets([seg], str(dub), 0, str(orig), 0, log=logs.append)
    assert [s.kind for s in out] == ["match"], (
        [(s.kind, s.a_start, s.a_end, s.offset) for s in out], logs)
    assert abs(out[0].offset + d) < 0.002, (out[0].offset, out[0].extra)
    assert not any("dividido" in m for m in logs), logs


@pytest.mark.ffmpeg
@pytest.mark.parametrize("codec", ["aac", "ac3"])
def test_primeira_janela_do_merge_nao_perde_o_primeiro_pacote(tmp_path, codec):
    """A 1ª janela do merge de filmes começa em 0:30 e a do outro arquivo, com
    a folga, em 30 - ALIGN_PAD = 0. Um `-ss 0.000` de entrada ainda busca, e
    o 1º pacote do MKV sumia (AAC a 48 kHz: 21 ms; AC3: 32 ms): a 1ª janela
    destoava das outras — e é ela que vira o tau1 do VersionMismatch."""
    x = _noise(200, seed=43)
    ref = _mkv(tmp_path / "ref.mkv", x, "flac")
    dub = _mkv(tmp_path / "dub.mkv", _delay(x, 1.25), codec)
    assert merger.ALIGN_START - merger.ALIGN_PAD <= 0
    tau, q = merger._measure_window(str(ref), 0, str(dub), 0, merger.ALIGN_START)
    assert abs(tau - 1.25) < 0.002 and q >= merger.ALIGN_MIN_QUALITY, (tau, q)


# -------------------- 2. piso do ruído --------------------

@pytest.mark.parametrize("win, radius, limiar", [
    (0.75, refine.SEARCH_RADIUS_S, refine.MIN_PEAK_QUALITY),   # bissecção fina
    (3.0, refine.SEARCH_RADIUS_S, refine.MIN_PEAK_QUALITY),    # junção / vizinhos
    (12.0, refine.SEARCH_RADIUS_S, refine.MIN_PEAK_QUALITY),   # perfil
    (12.0, merger.MAX_OFFSET_SECONDS, refine.SCAN_MIN_PEAK_QUALITY),  # scan
])
def test_limiar_de_cada_raio_fica_acima_do_ruido_puro(win, radius, limiar):
    """Dois sinais INDEPENDENTES: nenhum par pode passar do limiar do raio
    usado. E o limiar antigo (6) ficava dentro desse ruído em todos os
    raios (a tabela completa está no comentário de MIN_PEAK_QUALITY)."""
    rng = np.random.default_rng(61)
    n = 60 if radius > 1 else 150
    qs = np.array([merger.gcc_phat_delay_with_confidence(
        _noise(win, rng=rng), _noise(win, rng=rng), SR, max_tau=radius)[1]
        for _ in range(n)])
    assert qs.max() < limiar, (qs.max(), limiar)
    assert (qs >= 6.0).any(), qs.max()


@pytest.mark.ffmpeg
def test_ruido_puro_nao_vota_no_scan(tmp_path):
    """Dublado e original sem nada em comum: nenhuma janela do scan pode
    votar. Com o limiar antigo (6) votavam todas (pico 8-9 com a busca de
    ±60 s), cada uma com um offset aleatório."""
    dub = _wav(tmp_path / "dub.wav", _noise(1000, seed=51))
    orig = _wav(tmp_path / "orig.wav", _noise(1000, seed=52))
    _ok, pts = refine.scan_constant_offset(str(dub), 0, str(orig), 0, 1000.0, step=60.0)
    assert pts == [], pts


@pytest.mark.ffmpeg
def test_trecho_sem_correlacao_nao_tira_o_episodio_do_caminho_rapido(tmp_path):
    """Offset constante (dublado 0,4 s atrasado) com 2 min e 40 s de "música
    diferente" (ruído independente) no meio do dublado. As janelas dali não
    votam; as outras concordam e o episódio fica no caminho rápido. Antes
    as janelas do trecho votavam com offset aleatório e o episódio ia para
    o alinhador por conteúdo à toa."""
    x = _noise(1000, seed=53)
    d = _delay(x, 0.4)[:len(x)]
    d[int(400 * SR):int(560 * SR)] = _noise(160, seed=54)
    dub = _wav(tmp_path / "dub.wav", d)
    orig = _wav(tmp_path / "orig.wav", x)
    ok, pts = refine.scan_constant_offset(str(dub), 0, str(orig), 0, 1000.0, step=60.0)
    ts = [t for t, _, _ in pts]
    assert 450.0 not in ts and 510.0 not in ts, pts   # janelas dentro do trecho
    assert len(pts) >= 13, pts
    assert all(abs(o + 0.4) < 0.002 for _, o, _ in pts), pts
    assert ok
