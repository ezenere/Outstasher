"""Dublagem gravada fora do lugar no merge de filme (o defeito de
fix_interleaving): legenda esparsa numa entrada + a dublagem na outra.

Caso real: 11 trechos sem a dublagem (19 min) num filme cujas músicas não
tinham legenda; a amostra de 4 pontos da checagem antiga passou limpa.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

from services import merger

quiet = lambda *a, **k: None  # noqa: E731


# -------------------- medição no arquivo inteiro --------------------

def _streams(monkeypatch, pacotes):
    monkeypatch.setattr(merger, "ffprobe_json", lambda p: {
        "format": {"duration": "200"},
        "streams": [{"index": 0, "codec_type": "video"},
                    {"index": 1, "codec_type": "audio"},
                    {"index": 2, "codec_type": "audio"},
                    {"index": 3, "codec_type": "subtitle"}]})
    monkeypatch.setattr(merger, "_packet_order", lambda p: iter(pacotes))


def _sadio(t0, t1, faixas=(1, 2)):
    """Vídeo e áudios andando juntos, um pacote a cada 0,1 s."""
    out = []
    for k in range(t0 * 10, t1 * 10):
        out.append((0, k / 10))
        out += [(a, k / 10 + 0.02) for a in faixas]
    return out


def test_buraco_no_meio_do_arquivo_e_medido(monkeypatch):
    """A dublagem (1) some de 20 a 70 s e chega atrasada depois — o caso real.
    Qualquer amostra que não caia no buraco veria tudo limpo."""
    atrasados = [(1, 20 + k * 0.5) for k in range(100)]
    pacotes = _sadio(0, 20) + _sadio(20, 70, faixas=(2,)) + atrasados + _sadio(70, 200)
    _streams(monkeypatch, pacotes)
    lags = merger.interleave_lags("x.mkv")
    assert lags[0] >= 49
    assert lags[1] < 1


def test_faixa_adiantada_e_medida(monkeypatch):
    """O outro sentido: a dublagem gravada minutos ANTES do vídeo dela."""
    adiantados = [(1, 300.0 + k) for k in range(5)]
    _streams(monkeypatch, _sadio(0, 5) + adiantados + _sadio(5, 200))
    assert merger.interleave_lags("x.mkv")[0] >= 290


def test_audio_alem_do_ultimo_video_nao_conta(monkeypatch):
    """Dublagem que segue 20 s além do vídeo é normal, não intercalação."""
    cauda = [(1, 200.0 + k) for k in range(20)]
    _streams(monkeypatch, _sadio(0, 200) + cauda)
    assert max(merger.interleave_lags("x.mkv").values()) < 1


def test_faixa_que_nunca_aparece_e_infinita(monkeypatch):
    _streams(monkeypatch, _sadio(0, 50, faixas=(2,)))
    lags = merger.interleave_lags("x.mkv")
    assert lags[0] == float("inf") and lags[1] < 1


# -------------------- comando do mkvmerge --------------------

def _st(index, type_index=0):
    return {"index": index, "_type_index": type_index}


def test_mkvmerge_merge_cmd_monta_o_mesmo_arquivo_do_ffmpeg():
    cmd = merger.mkvmerge_merge_cmd(
        "out.mkv", ("orig.mkv", "dub.mkv"),
        {"src": 0, "stream": _st(0)},
        [{"src": 1, "stream": _st(1), "lang": "por", "title": "", "default": True},
         {"src": 0, "stream": _st(2, 0), "lang": "eng", "title": "Comentário",
          "default": False}],
        [{"src": 0, "stream": _st(5, 2), "lang": "por", "title": ""},
         {"src": 1, "stream": _st(3, 0), "lang": "por", "title": "Forçada"}],
        chapters_from=0, shifts_ms=(1015, 0))
    i_orig, i_dub = cmd.index("orig.mkv"), cmd.index("dub.mkv")
    orig, dub = cmd[3:i_orig], cmd[i_orig + 1:i_dub]
    assert cmd[:3] == ["mkvmerge", "-o", "out.mkv"]
    # seleção por entrada (ids = index do ffprobe)
    assert orig[orig.index("-d") + 1] == "0"
    assert orig[orig.index("-a") + 1] == "2" and orig[orig.index("-s") + 1] == "5"
    assert "-D" in dub and dub[dub.index("-a") + 1] == "1"
    assert dub[dub.index("-s") + 1] == "3"
    # o atraso (-itsoffset) vale para todas as faixas da entrada atrasada
    syncs = [orig[k + 1] for k, x in enumerate(orig) if x == "--sync"]
    assert sorted(syncs) == ["0:1015", "2:1015", "5:1015"]
    assert "--sync" not in dub
    # capítulos e tags globais só da entrada certa
    assert "--no-chapters" not in orig and "--no-chapters" in dub
    assert "--no-global-tags" in dub and "--no-global-tags" not in orig
    # flags e nomes
    assert "1:yes" in dub and "2:no" in orig
    assert "2:Comentário" in orig and "3:Forçada" in dub
    assert "--language" in dub and "1:por" in dub
    # ordem de saída: vídeo, áudios na ordem do merge, depois legendas
    assert cmd[-2:] == ["--track-order", "0:0,1:1,0:2,0:5,1:3"]


# -------------------- merge de verdade (ffmpeg real) --------------------

def _srt(path, cues):
    def t(s):
        return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d},000"
    path.write_text("".join(f"{n}\n{t(a)} --> {t(a + 2)}\nlinha {n}\n\n"
                            for n, a in enumerate(cues, 1)))
    return path


@pytest.fixture
def par_com_legenda_esparsa(tmp_path, monkeypatch):
    """Original com legenda SEM FALA de 20 s a 170 s + dublado; offset fixo
    de 0,5 s; orçamento de intercalação mínimo (delta de 10 s), como um
    REMUX 4K vê o de 1 GB."""
    srt = _srt(tmp_path / "eng.srt", [5, 10, 15, 170, 175, 180, 185])
    orig, dub = tmp_path / "orig.mkv", tmp_path / "dub.mkv"
    subprocess.run(["ffmpeg", "-v", "error", "-y",
                    "-f", "lavfi", "-i", "testsrc2=s=320x180:r=10:d=200",
                    "-f", "lavfi", "-i", "sine=f=440:d=200", "-i", str(srt),
                    "-map", "0", "-map", "1", "-map", "2",
                    "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "ac3",
                    "-c:s", "srt", "-metadata:s:a:0", "language=eng",
                    "-metadata:s:s:0", "language=eng", str(orig)], check=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y",
                    "-f", "lavfi", "-i", "testsrc2=s=160x90:r=10:d=200",
                    "-f", "lavfi", "-i", "sine=f=660:d=200",
                    "-map", "0", "-map", "1", "-c:v", "libx264", "-preset",
                    "ultrafast", "-c:a", "ac3", "-metadata:s:a:0", "language=por",
                    str(dub)], check=True)
    monkeypatch.setattr(merger, "_measure_window", lambda *a, **k: (0.5, 100.0))
    monkeypatch.setattr(merger, "MUX_BUFFER_GB", 1e-6)
    return orig, dub, tmp_path / "out.mkv"


def _sub_pts(path):
    p = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "s",
                        "-show_entries", "packet=pts_time", "-of", "csv=p=0",
                        str(path)], capture_output=True, text=True)
    return [float(x) for x in p.stdout.split()]


@pytest.mark.ffmpeg
def test_ffmpeg_com_legenda_na_entrada_propria_nao_desgarra(par_com_legenda_esparsa,
                                                            monkeypatch):
    orig, dub, out = par_com_legenda_esparsa
    monkeypatch.setattr(merger, "has_mkvmerge", lambda: False)
    logs = []
    res = merger.merge(str(orig), str(dub), str(out), "pt", log=logs.append)
    assert any("Separando 1 legenda" in x for x in logs)
    assert max(merger.interleave_lags(str(out)).values()) < merger.INTERLEAVE_CHECK_TOL_S
    assert not any("FROUXA" in n for n in res.notes)
    # a legenda leva o mesmo atraso da entrada dela (o original, +0,5 s)
    assert _sub_pts(out)[:3] == pytest.approx([5.5, 10.5, 15.5], abs=0.05)


@pytest.mark.ffmpeg
def test_sem_separar_a_legenda_o_defeito_aparece(par_com_legenda_esparsa, monkeypatch):
    """Controle: o teste acima só vale se o par REPRODUZ o defeito."""
    orig, dub, out = par_com_legenda_esparsa
    monkeypatch.setattr(merger, "has_mkvmerge", lambda: False)
    monkeypatch.setattr(merger, "isolate_subtitles", lambda *a, **k: None)
    res = merger.merge(str(orig), str(dub), str(out), "pt", log=quiet)
    assert merger.interleave_lags(str(out))[0] > 60
    assert any("FROUXA" in n for n in res.notes)


@pytest.mark.ffmpeg
@pytest.mark.skipif(shutil.which("mkvmerge") is None, reason="sem mkvmerge no PATH")
def test_copia_vai_pelo_mkvmerge(par_com_legenda_esparsa, ffprobe_streams):
    orig, dub, out = par_com_legenda_esparsa
    logs = []
    res = merger.merge(str(orig), str(dub), str(out), "pt", log=logs.append)
    assert any("Executando mkvmerge" in x for x in logs)
    assert "mkvmerge --sync" in res.mode
    assert max(merger.interleave_lags(str(out)).values()) < merger.INTERLEAVE_CHECK_TOL_S
    auds = ffprobe_streams(out, "audio")
    assert [(a.get("tags") or {}).get("language") for a in auds] == ["por", "eng"]
    assert [a["disposition"]["default"] for a in auds] == [1, 0]
    assert ffprobe_streams(out, "video")[0]["width"] == 320
    assert _sub_pts(out)[:3] == pytest.approx([5.5, 10.5, 15.5], abs=0.05)
    assert Path(out).exists()
