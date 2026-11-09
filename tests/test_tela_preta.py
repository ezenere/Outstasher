"""Tela preta nos trechos sem dublagem não sai do vídeo.

Caso real: o original tem ~1 s de tela preta (segundo 0, intervalo entre
atos, fim) que a fonte dublada não tem. Com a política "a cena sai do vídeo"
isso virava cut_video — e cortar no frame re-encodava o vídeo INTEIRO (num 4K
com Dolby Vision: 1h29 de GPU e o DV perdido para tirar 1,0 s de preto). Preto
não é cena: fica no vídeo, mudo, e o vídeo segue em stream copy.
"""
import json
import subprocess

import pytest

from services import advanced_merge
from services.series.align import black, edl as edl_mod, rules
from services.series.align.classify import Segment


def _video(path, partes, pix_fmt="yuv420p", vcodec="libx264", audio=True):
    """MKV com trechos (tipo, duração) emendados: 'preto' é tela preta,
    'imagem' é o testsrc2."""
    fonte = {"preto": "color=c=black:s=320x180:r=24:d={d}",
             "imagem": "testsrc2=s=320x180:r=24:d={d}"}
    ins, refs = [], []
    for i, (tipo, d) in enumerate(partes):
        ins += ["-f", "lavfi", "-i", fonte[tipo].format(d=d)]
        refs.append(f"[{i}:v]")
    dur = sum(d for _, d in partes)
    fc = "".join(refs) + f"concat=n={len(partes)}:v=1:a=0[v]"
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *ins]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"anoisesrc=color=pink:seed=3:duration={dur}"]
    cmd += ["-filter_complex", fc, "-map", "[v]"]
    if audio:
        cmd += ["-map", f"{len(partes)}:a", "-c:a", "ac3", "-b:a", "128k", "-ac", "2",
                "-metadata:s:a:0", "language=eng"]
    cmd += ["-c:v", vcodec, "-preset", "ultrafast", "-pix_fmt", pix_fmt,
            "-g", "48", "-keyint_min", "48"]
    if vcodec == "libx265":
        cmd += ["-x265-params", "log-level=error"]
    subprocess.run(cmd + [str(path)], check=True)
    return path


def _cfg_corte(**kw):
    cfg = dict(advanced_merge.DEFAULTS, undubbed="cut", cut_min_s=1.0)
    cfg.update(kw)
    return cfg


# ---------------------------------------------------------------- medição

@pytest.mark.ffmpeg
def test_mede_o_preto_so_dentro_do_trecho(tmp_path):
    f = _video(tmp_path / "o.mkv", [("preto", 2), ("imagem", 3),
                                    ("preto", 1.5), ("imagem", 2)],
               audio=False)
    # o último frame preto não entra (o filtro fecha o intervalo no pts dele):
    # ~42 ms a menos, abaixo de qualquer limite que importa
    assert black.black_seconds(str(f), 0.0, 2.0) == pytest.approx(2.0, abs=0.06)
    assert black.black_seconds(str(f), 5.0, 6.5) == pytest.approx(1.5, abs=0.06)
    # metade preto, metade imagem
    assert black.black_seconds(str(f), 1.5, 3.0) == pytest.approx(0.5, abs=0.06)
    assert black.black_seconds(str(f), 2.0, 5.0) == 0.0
    # trecho além do fim do arquivo: nada medido como preto
    assert black.black_seconds(str(f), 8.6, 9.5) == 0.0


@pytest.mark.ffmpeg
def test_mede_preto_em_10_bits(tmp_path, real_encoders):
    """O 4K HDR do caso real é 10 bits: preto é luma 64, não 16."""
    if "libx265" not in real_encoders:
        pytest.skip("ffmpeg sem libx265")
    f = _video(tmp_path / "o10.mkv", [("preto", 1), ("imagem", 2)],
               pix_fmt="yuv420p10le", vcodec="libx265", audio=False)
    assert black.black_seconds(str(f), 0.0, 1.0) == pytest.approx(1.0, abs=0.06)
    assert black.black_seconds(str(f), 1.0, 3.0) == 0.0


def test_ffmpeg_falhando_nao_marca(tmp_path):
    assert black.black_seconds(str(tmp_path / "nao-existe.mkv"), 0.0, 1.0) is None
    segs = [Segment("gap_orig", 0.0, 0.0, 0.0, 1.0)]
    assert black.mark(segs, str(tmp_path / "nao-existe.mkv")) == 0
    assert "picture_s" not in segs[0].extra


def test_mark_escolhe_o_que_medir(monkeypatch):
    chamadas = []

    def fake(path, t0, t1):
        chamadas.append((t0, t1))
        return t1 - t0 - 0.04          # tudo preto, menos um frame

    monkeypatch.setattr(black, "black_seconds", fake)
    segs = [
        Segment("gap_orig", 0.0, 0.0, 0.0, 1.0),          # mede
        Segment("match", 0.0, 50.0, 1.0, 51.0, offset=1.0),  # não é trecho sem dublagem
        Segment("gap_orig", 50.0, 50.0, 51.0, 51.3),      # ruído do alinhador
        Segment("gap_orig", 50.0, 50.0, 60.0, 100.0),     # 40 s: é cena, nem decodifica
        Segment("replaced", 50.0, 52.0, 100.0, 102.0),    # mede
        Segment("gap_orig", 52.0, 52.0, 102.0, 103.0),    # já medido (EDL nova)
        Segment("gap_dub", 52.0, 60.0, None, None),
    ]
    segs[5].extra["picture_s"] = 0.9
    logs = []
    assert black.mark(segs, "orig.mkv", log=logs.append) == 2
    assert chamadas == [(0.0, 1.0), (100.0, 102.0)]
    assert segs[0].extra["picture_s"] == pytest.approx(0.04)
    assert segs[5].extra["picture_s"] == 0.9
    assert "picture_s" not in segs[3].extra
    assert logs and "2 de 2" in logs[0]


def test_mark_limita_ao_fim_do_arquivo(monkeypatch):
    """A grade de 0,25 s do alinhador passa do fim do arquivo: a medida vai
    até o fim real, e o pedaço que não existe conta como IMAGEM (na dúvida,
    a política continua podendo cortar)."""
    monkeypatch.setattr(black, "black_seconds", lambda p, t0, t1: t1 - t0)
    segs = [Segment("gap_orig", 0.0, 0.0, 99.0, 100.25)]
    black.mark(segs, "orig.mkv", dur_orig=100.0)
    assert segs[0].extra["picture_s"] == pytest.approx(0.25)


def test_medida_viaja_na_edl():
    segs = [Segment("gap_orig", 0.0, 0.0, 0.0, 1.0)]
    segs[0].extra["picture_s"] = 0.042
    edl = edl_mod.build(segs, "S01E01", "dub.mkv", 10.0, "orig.mkv", 11.0)
    assert edl_mod.segments(edl)[0].extra["picture_s"] == 0.042


# ---------------------------------------------------------------- política

def _acoes(segs, cfg):
    out, _ = rules.apply_rules(segs, advanced_merge.default_rules(cfg), 3000.0)
    return [s.extra.get("action") for s in out]


def _gap(b0, b1, picture=None):
    s = Segment("gap_orig", 0.0, 0.0, b0, b1)
    if picture is not None:
        s.extra["picture_s"] = picture
    return s


def test_politica_de_corte_poupa_tela_preta():
    segs = [
        _gap(0.0, 1.0, picture=0.04),      # preto do segundo 0 (caso real)
        _gap(800.0, 801.4, picture=0.0),   # intervalo entre atos
        _gap(900.0, 902.0, picture=2.0),   # cena de verdade
        _gap(1000.0, 1001.0),              # não medido: corta, como antes
        _gap(1100.0, 1103.0, picture=0.8),  # fade + preto: imagem < mínimo
        _gap(1200.0, 1203.0, picture=1.2),  # imagem >= mínimo: é cena
        _gap(1300.0, 1300.6, picture=0.6),  # abaixo do mínimo: mudo, como antes
    ]
    assert _acoes(segs, _cfg_corte()) == [
        "silence", "silence", "cut_video", "cut_video",
        "silence", "cut_video", "silence"]


def test_corta_tudo_ainda_poupa_o_preto():
    """cut_min_s 0 ('corta tudo'): um frame de fade não pode valer como cena."""
    segs = [_gap(0.0, 1.0, picture=0.3), _gap(10.0, 11.0, picture=0.6)]
    assert _acoes(segs, _cfg_corte(cut_min_s=0.0)) == ["silence", "cut_video"]


def test_minimo_alto_poupa_preto_com_pouca_imagem():
    segs = [_gap(0.0, 20.0, picture=4.0), _gap(30.0, 50.0, picture=6.0)]
    assert _acoes(segs, _cfg_corte(cut_min_s=5.0)) == ["silence", "cut_video"]


def test_decisao_do_usuario_vence_o_preto():
    seg = _gap(0.0, 1.0, picture=0.0)
    seg.extra["action"] = "cut_video"
    assert _acoes([seg], _cfg_corte()) == ["cut_video"]


def test_outras_politicas_nao_mudam():
    def segs():
        return [_gap(0.0, 1.0, picture=0.0), _gap(10.0, 12.0, picture=2.0)]

    cfg = dict(advanced_merge.DEFAULTS, undubbed="silence")
    assert _acoes(segs(), cfg) == ["silence", "silence"]
    cfg = dict(advanced_merge.DEFAULTS, undubbed="fill")
    assert _acoes(segs(), cfg) == [None, None]


# ---------------------------------------------------------- preparo do render

REENCODE = {"video_codec": "hevc", "hw_accel": "none", "quality_mode": "crf",
            "crf": 30, "preset": "default"}


def test_so_preto_nao_reencoda(monkeypatch):
    monkeypatch.setattr(black, "black_seconds", lambda p, t0, t1: t1 - t0 - 0.04)
    segs = [_gap(0.0, 1.0), Segment("match", 0.0, 50.0, 1.0, 51.0, offset=1.0),
            _gap(51.0, 52.2)]
    logs = []
    out, kw = advanced_merge.prepare_render(
        segs, _cfg_corte(reencode=REENCODE), "orig.mkv", 52.2, 50.0, logs.append)
    assert [s.extra.get("action") for s in out] == ["silence", None, "silence"]
    assert "video_reencode" not in kw
    assert any("2 trecho(s) de tela preta" in m and "stream copy" in m for m in logs)


def test_cena_de_verdade_ainda_reencoda(monkeypatch):
    preto = {0.0: True, 51.0: False}
    monkeypatch.setattr(black, "black_seconds",
                        lambda p, t0, t1: (t1 - t0) if preto[t0] else 0.0)
    segs = [_gap(0.0, 1.0), Segment("match", 0.0, 50.0, 1.0, 51.0, offset=1.0),
            _gap(51.0, 60.0)]
    logs = []
    out, kw = advanced_merge.prepare_render(
        segs, _cfg_corte(reencode=REENCODE), "orig.mkv", 60.0, 50.0, logs.append)
    assert [s.extra.get("action") for s in out] == ["silence", None, "cut_video"]
    assert kw["video_reencode"] == {"convert": REENCODE}
    assert any("1 trecho(s) de tela preta" in m and "stream copy" not in m for m in logs)


def test_politica_sem_corte_nem_mede(monkeypatch):
    def proibido(*a):
        raise AssertionError("não devia decodificar nada")

    monkeypatch.setattr(black, "black_seconds", proibido)
    for und in ("silence", "fill"):
        segs = [_gap(0.0, 1.0)]
        cfg = dict(advanced_merge.DEFAULTS, undubbed=und, reencode=REENCODE)
        _, kw = advanced_merge.prepare_render(segs, cfg, "orig.mkv", 10.0, 9.0)
        assert "video_reencode" not in kw
        assert advanced_merge.mark_black(segs, cfg, "orig.mkv", 10.0) == 0


@pytest.mark.ffmpeg
def test_render_mantem_o_preto_e_o_video_em_copia(tmp_path):
    """Fim a fim: original com 1 s de preto no começo que o dublado não tem,
    política de corte COM re-encode configurado. A saída tem a duração do
    original e o vídeo dele em stream copy (h264, não o hevc do re-encode)."""
    from services.series.align import render as render_mod
    orig = _video(tmp_path / "orig.mkv", [("preto", 1), ("imagem", 6)])
    dub = _video(tmp_path / "dub.mkv", [("imagem", 6)])
    segs = [Segment("gap_orig", 0.0, 0.0, 0.0, 1.0),
            Segment("match", 0.0, 6.0, 1.0, 7.0, offset=1.0)]
    logs = []
    segs, kw = advanced_merge.prepare_render(
        segs, _cfg_corte(reencode=REENCODE), str(orig), 7.0, 6.0, logs.append)
    assert segs[0].extra["action"] == "silence"
    assert "video_reencode" not in kw
    out = tmp_path / "out.mkv"
    render_mod.render(segs, str(dub), str(orig), str(out), "pt",
                      log=logs.append, **kw)
    info = json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format",
         "-show_streams", str(out)], capture_output=True, text=True,
        check=True).stdout)
    assert abs(float(info["format"]["duration"]) - 7.0) < 0.3
    video = next(s for s in info["streams"] if s["codec_type"] == "video")
    assert video["codec_name"] == "h264"
    assert not any("cut_video" in m or "Re-encodando" in m for m in logs)
