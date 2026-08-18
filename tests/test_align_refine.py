"""Refino por ÁUDIO: perfil de offset, corte em silêncio, wobbles e
'substituída' resolvida pelo áudio — com áudio REAL (ffmpeg).

Reproduz o caso Mr Robot S01E01: o dublado tem uma edição de 2 frames numa
junção de intervalo comercial (offset muda ~70 ms no meio do episódio) que o
vídeo a 4 fps não enxerga; o corte tem que ser detectado e cair num silêncio.
"""
import subprocess

import pytest

from services.series.align import refine
from services.series.align.classify import Segment

pytestmark = pytest.mark.ffmpeg


def _audio(path, dur, seed, gaps=()):
    """Áudio de ruído rosa (correlaciona) com silêncios em `gaps`
    ([(início, fim), ...]) — os 'respiros' entre falas."""
    vol = "".join(f",volume=enable='between(t,{a},{b})':volume=0" for a, b in gaps)
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", f"anoisesrc=color=pink:seed={seed}:duration={dur}",
         "-af", f"aformat=channel_layouts=stereo{vol}",
         "-c:a", "pcm_s16le", str(path)], check=True)
    return path


def _dub_with_edit(orig_wav, path, cut_at=60.0, drop=0.070):
    """'Dublado' = o mesmo áudio com `drop` s REMOVIDOS em cut_at (junção
    de intervalo comercial): antes offset 0, depois o dublado adianta."""
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(orig_wav),
         "-filter_complex",
         f"[0:a]atrim=0:{cut_at},asetpts=PTS-STARTPTS[a1];"
         f"[0:a]atrim={cut_at + drop},asetpts=PTS-STARTPTS[a2];"
         f"[a1][a2]concat=n=2:v=0:a=1[a]",
         "-map", "[a]", "-c:a", "pcm_s16le", str(path)], check=True)
    return path


def test_edicao_pequena_detectada_e_corte_em_silencio(tmp_path):
    # silêncios (respiros) a cada ~20 s; o corte real está em 60 s, o
    # silêncio mais próximo é 61.0-61.6
    gaps = [(19.0, 19.6), (40.0, 40.6), (61.0, 61.6), (80.0, 80.6), (100.0, 100.6)]
    orig = _audio(tmp_path / "orig.wav", 120, seed=1, gaps=gaps)
    dub = _dub_with_edit(orig, tmp_path / "dub.wav", cut_at=60.0, drop=0.070)
    seg = Segment("match", 0.0, 119.5, 0.0, 119.5, offset=0.0, slope=1.0)
    logs = []
    out = refine.refine_offsets([seg], str(dub), 0, str(orig), 0, log=logs.append)
    matches = [s for s in out if s.kind == "match"]
    assert len(matches) == 2, [(s.kind, s.a_start, s.a_end, s.offset) for s in out]
    a, b = matches
    # antes: offset ~0; depois: o dublado perdeu 70 ms -> orig fica 70 ms
    # "atrás" (offset = b - a = +0.070)
    assert abs(a.offset) < 0.010, a.offset
    assert abs(b.offset - 0.070) < 0.010, b.offset
    # o corte caiu no silêncio de 61.0-61.6 (no dublado, o silêncio está
    # 70 ms antes: 60.93-61.53), não no meio do "diálogo"
    assert 60.8 <= a.a_end <= 61.6, a.a_end
    assert any("encaixado no silêncio" in l for l in logs)


def test_offset_constante_nao_divide(tmp_path):
    orig = _audio(tmp_path / "orig.wav", 90, seed=2, gaps=[(30, 30.5), (60, 60.5)])
    # dublado = orig deslocado 40 ms (constante)
    dub = tmp_path / "dub.wav"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(orig),
         "-af", "adelay=40|40", "-c:a", "pcm_s16le", str(dub)], check=True)
    seg = Segment("match", 0.0, 89.0, 0.0, 89.0, offset=0.0, slope=1.0)
    out = refine.refine_offsets([seg], str(dub), 0, str(orig), 0, log=lambda m: None)
    assert [s.kind for s in out] == ["match"]
    # dublado atrasado 40 ms -> offset (b - a) = -0.040
    assert abs(out[0].offset + 0.040) < 0.010, out[0].offset


def test_wobble_do_video_e_fundido(tmp_path):
    # estrutura que o vídeo produz num plano parado: match / gap 0.5 / match
    # curto com offset 0.25 / gap 0.5 / match — vizinhos com o mesmo offset
    segs = [
        Segment("match", 0.0, 40.0, 0.0, 40.0, offset=0.0),
        Segment("gap_orig", 40.0, 40.0, 40.0, 40.5),
        Segment("match", 40.0, 44.0, 40.5, 44.5, offset=0.5),
        Segment("gap_dub", 44.0, 44.5, 44.5, 44.5),
        Segment("match", 44.5, 90.0, 44.5, 90.0, offset=0.0),
    ]
    out = refine.collapse_wobbles(segs)
    assert [(s.kind, s.a_start, s.a_end) for s in out] == [("match", 0.0, 90.0)]


def test_substituida_com_audio_continuo_vira_match(tmp_path):
    orig = _audio(tmp_path / "orig.wav", 60, seed=3, gaps=[(20, 20.4), (40, 40.4)])
    dub = tmp_path / "dub.wav"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-i", str(orig), "-c:a", "pcm_s16le", str(dub)], check=True)
    segs = [
        Segment("match", 0.0, 25.0, 0.0, 25.0, offset=0.0),
        Segment("replaced", 25.0, 30.0, 25.0, 30.0),   # vídeo diverge, áudio não
        Segment("match", 30.0, 59.0, 30.0, 59.0, offset=0.0),
    ]
    logs = []
    out = refine.refine_offsets(segs, str(dub), 0, str(orig), 0, log=logs.append)
    assert all(s.kind == "match" for s in out), [s.kind for s in out]
    assert any("vira match" in l for l in logs)


def test_scan_constant_offset(tmp_path):
    orig = _audio(tmp_path / "orig.wav", 700, seed=4)
    dub = _dub_with_edit(orig, tmp_path / "dub.wav", cut_at=350.0, drop=0.100)
    ok, pts = refine.scan_constant_offset(str(dub), 0, str(orig), 0, 700.0,
                                          step=120.0)
    assert not ok and len(pts) >= 3
    same = tmp_path / "same.wav"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-i", str(orig), "-c:a", "pcm_s16le", str(same)], check=True)
    ok2, _ = refine.scan_constant_offset(str(same), 0, str(orig), 0, 700.0,
                                         step=120.0)
    assert ok2


def test_juncao_com_dublado_a_mais_nao_puxa_original(tmp_path):
    """Caso real (E03 21:00): o dublado tem ~0,6 s a mais numa junção; o
    original segue direto. O corte tem que cair num silêncio do dublado e o
    original ficar CONTÍNUO (sem buraco preenchido com áudio original)."""
    dub = _audio(tmp_path / "dub.wav", 70, seed=7, gaps=[(29.8, 30.4)])
    segs = [
        Segment("match", 0.0, 30.0, 0.0, 30.0, offset=0.0),
        Segment("gap_dub", 30.0, 30.6, 30.0, 30.0),      # 0,6 s só no dublado
        Segment("match", 30.6, 60.0, 30.0, 59.4, offset=-0.6),
    ]
    logs = []
    out = refine._tighten_extra_dub(segs, str(dub), 0, log=logs.append)
    assert [s.kind for s in out] == ["match", "match"]
    a, b = out
    assert abs(a.a_end - 30.1) < 0.4          # corte no silêncio 29.8-30.4
    assert abs(b.a_start - (a.a_end + 0.6)) < 1e-6  # pula o excesso do dublado
    assert abs(b.b_start - a.b_end) < 1e-6    # original contínuo
    assert any("junção" in l for l in logs)
