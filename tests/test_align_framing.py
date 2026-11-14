"""Enquadramento: um lado é RECORTE do outro (ffmpeg real).

Caso de campo: dublado 1088x720 = original 1920x1080 sem 288 px da esquerda.
O fingerprint do quadro inteiro não casava nada; com o original recortado
igual, casa. Aqui o par é sintético: o "dublado" é o original sem a faixa da
esquerda, na proporção 1,51, e atrasado 2 s.
"""
import subprocess

import pytest

from services.series.align import engine, framing

pytestmark = pytest.mark.ffmpeg

DUR = 200


def _orig(path):
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", f"testsrc2=s=320x180:d={DUR}:r=10",
         "-c:v", "libx264", "-preset", "ultrafast", "-crf", "26",
         "-pix_fmt", "yuv420p", str(path)], check=True)
    return path


def _recortado(src, path, delay=2.0):
    """Sem os 48 px da esquerda (272x180 = 1,51), reescalado e atrasado."""
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", f"color=c=black:s=204x136:d={delay}:r=10",
         "-i", str(src),
         "-filter_complex",
         "[1:v]crop=272:180:48:0,scale=204:136,setsar=1[v];[0:v][v]concat=n=2:v=1[o]",
         "-map", "[o]", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "26",
         "-pix_fmt", "yuv420p", str(path)], check=True)
    return path


def test_acha_o_recorte_do_original(tmp_path):
    orig = _orig(tmp_path / "orig.mkv")
    dub = _recortado(orig, tmp_path / "dub.mkv")
    ca, cb, nota = framing.match_framing(str(dub), None, DUR + 2.0,
                                         str(orig), None, float(DUR))
    assert ca is None and nota, (ca, cb, nota)
    w, h, x, y = (int(v) for v in cb.split(":"))
    assert abs(w - 272) <= 8 and h >= 172 and abs(x - 48) <= 8 and y <= 4, cb


def test_mesma_proporcao_nao_faz_nada(tmp_path, monkeypatch):
    orig = _orig(tmp_path / "orig.mkv")
    monkeypatch.setattr(framing, "_thumbs", lambda *a, **k: pytest.fail("decodificou"))
    assert framing.match_framing(str(orig), None, DUR, str(orig), None, DUR) == (None, None, None)


def test_alinhador_casa_par_recortado(tmp_path):
    """Sem o enquadramento este par dava AlignConflict ("quase nada casa")."""
    orig = _orig(tmp_path / "orig.mkv")
    dub = _recortado(orig, tmp_path / "dub.mkv")
    out = engine.align_pair(str(dub), str(orig), "S01E01")
    assert out["framing"]["orig"] and out["framing"]["dub"] is None, out.get("framing")
    casado = sum(s["a_end"] - s["a_start"] for s in out["segments"] if s["kind"] == "match")
    assert casado >= 0.9 * DUR, out["segments"]
    offs = [s["offset"] for s in out["segments"] if s["kind"] == "match"]
    assert all(abs(o + 2.0) < 0.5 for o in offs), offs   # dub atrasado 2 s


def test_candidatos_cobrem_o_recorte_lateral():
    cands = framing._candidates(1.511, 1.778)
    alvo = (288 / 1920, 0.0, 1632 / 1920, 1.0)
    assert any(abs(c[0] - alvo[0]) < 0.01 and abs(c[2] - alvo[2]) < 0.01 and c[3] == 1.0
               for c in cands)
