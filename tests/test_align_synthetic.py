"""Alinhador de ponta a ponta com VÍDEO REAL (ffmpeg): estágios 0-3.

Gera pares de episódios sintéticos com divergências CONHECIDAS (re-encode
puro; corte no meio) e confere que a EDL descreve exatamente o que foi feito.
É a validação de campo do fingerprint + DP + classificação — nada stubado.
"""
import subprocess

import pytest

from services.series.align import edl as edl_mod, engine

pytestmark = pytest.mark.ffmpeg


def _base_video(path, dur=60, size="320x180"):
    """Vídeo base: testsrc2 tem formas em movimento — cada janela de 250 ms
    tem estrutura própria, então o dHash distingue posições no tempo."""
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", f"testsrc2=s={size}:d={dur}:r=24",
         "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
         "-pix_fmt", "yuv420p", str(path)], check=True)
    return path


def _reencoded(src, path, size="480x270"):
    """Re-encode com outra resolução/qualidade (encodes diferentes do MESMO
    conteúdo — o caso 'limpo' de um release BD vs WEB)."""
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
         "-vf", f"scale={size}", "-c:v", "libx264", "-preset", "ultrafast",
         "-crf", "23", "-pix_fmt", "yuv420p", str(path)], check=True)
    return path


def _with_cut(src, path, cut_start=20, cut_end=30):
    """Versão com [cut_start, cut_end) REMOVIDO (censura/corte de exibição)."""
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
         "-filter_complex",
         f"[0:v]trim=0:{cut_start},setpts=PTS-STARTPTS[v1];"
         f"[0:v]trim={cut_end},setpts=PTS-STARTPTS[v2];"
         f"[v1][v2]concat=n=2:v=1[v]",
         "-map", "[v]", "-c:v", "libx264", "-preset", "ultrafast",
         "-crf", "23", "-pix_fmt", "yuv420p", str(path)], check=True)
    return path


def test_par_identico_e_limpo(tmp_path):
    base = _base_video(tmp_path / "base.mkv")
    other = _reencoded(base, tmp_path / "reenc.mkv")
    edl = engine.align_pair(str(base), str(other), episode="S01E01",
                            dump_png=str(tmp_path / "m.png"))
    st = edl_mod.stats(edl)
    assert st["match_pct"] > 95, edl["segments"]
    assert not st["review_required"]
    # o PNG de debug saiu válido
    assert (tmp_path / "m.png").read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_corte_no_meio_vira_gap_do_dublado(tmp_path):
    base = _base_video(tmp_path / "base.mkv")          # lado dublado: completo
    cut = _with_cut(base, tmp_path / "cut.mkv")        # original sem [20,30)
    edl = engine.align_pair(str(base), str(cut), episode="S01E01")
    segs = edl["segments"]
    gaps = [s for s in segs if s["kind"] == "gap_dub"]
    assert gaps, segs
    gap = max(gaps, key=lambda s: s["a_end"] - s["a_start"])
    # o trecho removido: ~10 s começando perto de 20 s (tolerância de 2 s —
    # o dHash trabalha a 4 fps e o encode borra a fronteira)
    assert abs((gap["a_end"] - gap["a_start"]) - 10.0) < 2.0, gap
    assert abs(gap["a_start"] - 20.0) < 2.0, gap
    # matches antes (offset ~0) e depois (offset ~-10 s) do corte
    matches = [s for s in segs if s["kind"] == "match"]
    assert len(matches) >= 2, segs
    assert abs(matches[0]["offset"]) < 1.0
    assert abs(matches[-1]["offset"] + 10.0) < 1.5
    assert not edl["review"]["required"]


def test_arquivos_sem_relacao_viram_conflito(tmp_path):
    a = _base_video(tmp_path / "a.mkv", dur=40)
    # mandelbrot: conteúdo sem NENHUMA relação com testsrc2
    b = tmp_path / "b.mkv"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "mandelbrot=s=320x180:r=24", "-t", "40",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
         str(b)], check=True)
    with pytest.raises(engine.AlignConflict, match="ordem|casa"):
        engine.align_pair(str(a), str(b), episode="S01E01")


def test_duracoes_incompativeis_viram_conflito(tmp_path):
    a = _base_video(tmp_path / "a.mkv", dur=30)
    b = _base_video(tmp_path / "b.mkv", dur=60)
    with pytest.raises(engine.AlignConflict, match="fundidos|divididos"):
        engine.align_pair(str(a), str(b))
