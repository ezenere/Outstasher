"""Renderer de EDL (merge por segmentos) + mecânica do gate de revisão.

Os testes de render constroem a EDL À MÃO (a detecção já é coberta por
test_align_*) e conferem o RESULTADO de verdade: duração, faixas e capítulos
(os do ORIGINAL, preservados) via ffprobe. O gate de revisão é exercitado no
dict do job.
"""
import json
import subprocess

import pytest

from services.series import pipeline
from services.series.align.classify import Segment
from services.series.align import render as render_mod, rules as rules_mod


def _media(path, dur, seed, size="320x180", chapters=None):
    """Vídeo testsrc2 + áudio de RUÍDO (aperiódico: correlaciona de verdade,
    ao contrário de senoide) — cada seed é uma 'dublagem' diferente.
    chapters: lista de (início, fim, título) gravada no MKV."""
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           "-f", "lavfi", "-i", f"testsrc2=s={size}:d={dur}:r=24",
           "-f", "lavfi", "-i", f"anoisesrc=color=pink:seed={seed}:duration={dur}"]
    if chapters:
        meta = path.with_suffix(".ffmeta")
        lines = [";FFMETADATA1"]
        for a, b, t in chapters:
            lines += ["[CHAPTER]", "TIMEBASE=1/1000", f"START={int(a * 1000)}",
                      f"END={int(b * 1000)}", f"title={t}"]
        meta.write_text("\n".join(lines) + "\n", encoding="utf-8")
        cmd += ["-i", str(meta), "-map_chapters", "2"]
    cmd += ["-map", "0:v", "-map", "1:a",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "ac3", "-b:a", "128k", "-ac", "2",
            "-metadata:s:a:0", "language=eng", str(path)]
    subprocess.run(cmd, check=True)
    return path


def _probe(path):
    p = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format",
         "-show_streams", "-show_chapters", str(path)],
        capture_output=True, text=True, check=True)
    return json.loads(p.stdout)


@pytest.mark.ffmpeg
def test_render_gap_orig_preenche_e_preserva_capitulos_do_original(tmp_path):
    """Original 60 s; dublado sem [20,30): o buraco é preenchido com o áudio
    ORIGINAL. Os capítulos do original passam intactos — e NENHUM capítulo
    de auditoria ("preenchido") é criado (poluía a mídia)."""
    orig = _media(tmp_path / "orig.mkv", 60, seed=1,
                  chapters=[(0, 30, "Abertura"), (30, 60, "Ato 1")])
    dub = _media(tmp_path / "dub.mkv", 50, seed=2)
    segs = [
        Segment("match", 0.0, 20.0, 0.0, 20.0, offset=0.0),
        Segment("gap_orig", 20.0, 20.0, 20.0, 30.0),
        Segment("match", 20.0, 50.0, 30.0, 60.0, offset=10.0),
    ]
    out = tmp_path / "out.mkv"
    render_mod.render(segs, str(dub), str(orig), str(out), "pt", log=lambda m: None)

    info = _probe(out)
    assert abs(float(info["format"]["duration"]) - 60.0) < 1.0
    audios = [s for s in info["streams"] if s["codec_type"] == "audio"]
    # áudio original (copy) + faixa dublada remontada
    assert len(audios) == 2
    assert audios[1].get("tags", {}).get("language") in ("por", "pt")
    chapters = info.get("chapters", [])
    titles = [c.get("tags", {}).get("title", "") for c in chapters]
    assert titles == ["Abertura", "Ato 1"], titles
    assert abs(float(chapters[1]["start_time"]) - 30.0) < 0.05


@pytest.mark.ffmpeg
def test_render_gap_dub_descarta_material_extra(tmp_path):
    """Dublado tem 10 s a mais (recap): o material some — a timeline é a do
    original — e não há preenchimento nenhum."""
    orig = _media(tmp_path / "orig.mkv", 50, seed=1)
    dub = _media(tmp_path / "dub.mkv", 60, seed=2)
    segs = [
        Segment("match", 0.0, 20.0, 0.0, 20.0, offset=0.0),
        Segment("gap_dub", 20.0, 30.0, 20.0, 20.0),
        Segment("match", 30.0, 60.0, 20.0, 50.0, offset=-10.0),
    ]
    out = tmp_path / "out.mkv"
    render_mod.render(segs, str(dub), str(orig), str(out), "pt", log=lambda m: None)

    info = _probe(out)
    assert abs(float(info["format"]["duration"]) - 50.0) < 1.0
    assert not info.get("chapters")


@pytest.mark.ffmpeg
def test_render_replaced_com_acao(tmp_path):
    """Cena substituída com ação use_dub: o áudio dublado entra mesmo o
    conteúdo divergindo (decisão do usuário)."""
    orig = _media(tmp_path / "orig.mkv", 40, seed=1)
    dub = _media(tmp_path / "dub.mkv", 40, seed=2)
    rep = Segment("replaced", 10.0, 20.0, 10.0, 20.0)
    rep.extra["action"] = "use_dub"
    segs = [
        Segment("match", 0.0, 10.0, 0.0, 10.0, offset=0.0),
        rep,
        Segment("match", 20.0, 40.0, 20.0, 40.0, offset=0.0),
    ]
    out = tmp_path / "out.mkv"
    render_mod.render(segs, str(dub), str(orig), str(out), "pt", log=lambda m: None)
    info = _probe(out)
    assert abs(float(info["format"]["duration"]) - 40.0) < 1.0
    assert not info.get("chapters")  # original sem capítulos: saída sem capítulos


# -------------------- regras de revisão --------------------

def _seg(kind, a0, a1, b0=None, b1=None):
    return Segment(kind, a0, a1, b0 if b0 is not None else a0,
                   b1 if b1 is not None else a1)


def test_apply_rules_por_forma():
    rules = [
        {"when": {"kind": "gap_dub", "position": "start", "min_len": 20,
                  "max_len": 90}, "action": "accept"},
        {"when": {"kind": "replaced"}, "action": "fill_original"},
    ]
    segs = [
        _seg("gap_dub", 0.0, 43.0),        # recap no início: casa com a regra 1
        _seg("match", 43.0, 500.0),
        _seg("replaced", 500.0, 515.0),    # casa com a regra 2
        _seg("gap_dub", 600.0, 640.0),     # no MEIO: não casa com a regra 1
    ]
    segs, needs = rules_mod.apply_rules(segs, rules, duration_a=1300.0)
    assert segs[0].extra["action"] == "accept"
    assert segs[2].extra["action"] == "fill_original"
    assert "action" not in segs[3].extra
    assert needs is False  # o único replaced foi resolvido pela regra


def test_apply_rules_replaced_sem_acao_exige_revisao():
    segs = [_seg("replaced", 10.0, 20.0)]
    _, needs = rules_mod.apply_rules(segs, [], duration_a=100.0)
    assert needs is True


# -------------------- gate de revisão (job) --------------------

def _review_job():
    edl = {
        "version": 1, "episode": "S01E01",
        "source_dub": {"path": "d.mkv", "duration": 100.0},
        "source_orig": {"path": "o.mkv", "duration": 100.0},
        "segments": [
            {"kind": "match", "a_start": 0.0, "a_end": 50.0, "b_start": 0.0,
             "b_end": 50.0, "offset": 0.0, "residual": 3.0, "confidence": 0.9,
             "slope": 1.0, "note": ""},
            {"kind": "replaced", "a_start": 50.0, "a_end": 60.0,
             "b_start": 50.0, "b_end": 60.0, "offset": None, "residual": 64.0,
             "confidence": 0.0, "slope": None, "note": "revisar"},
        ],
        "review": {"required": True, "flagged": [
            {"a_start": 50.0, "a_end": 60.0, "reason": "replaced"}]},
    }
    return {
        "id": "revjob", "media_type": "tv", "language": "pt",
        "status": "awaiting", "detail": "", "progress": {},
        "episodes": {"S01E01": {"season": 1, "episode": 1, "name": "Ep",
                                "air_date": None, "runtime": None,
                                "state": "review", "src": {}, "output": None,
                                "error": None, "edl": edl}},
        "torrents": [], "awaiting": {"reason": "alignment_review",
                                     "payload": {}},
        "report": None, "created_at": "2026-08-16T00:00:00",
    }


def test_apply_review_acao_explicita(temp_db):
    job = _review_job()
    pipeline._apply_review(job, {"actions": {"S01E01": {"1": "fill_original"}}})
    ep = job["episodes"]["S01E01"]
    assert ep["state"] == "downloaded"  # volta para a fila de merge (render)
    assert ep["edl"]["segments"][1]["action"] == "fill_original"
    assert ep["edl"]["review"]["required"] is False


def test_apply_review_por_regra(temp_db):
    job = _review_job()
    pipeline._apply_review(job, {"rules": [
        {"when": {"kind": "replaced"}, "action": "silence"}]})
    ep = job["episodes"]["S01E01"]
    assert ep["state"] == "downloaded"
    assert ep["edl"]["segments"][1]["action"] == "silence"
    assert job["review_rules"]  # regra ficou salva para os próximos episódios


def test_apply_review_skip_falha_o_episodio(temp_db):
    job = _review_job()
    pipeline._apply_review(job, {"skip": ["S01E01"]})
    ep = job["episodes"]["S01E01"]
    assert ep["state"] == "failed"
    assert "pulada" in ep["error"]


def test_apply_review_sem_decisao_mantem_revisao(temp_db):
    job = _review_job()
    pipeline._apply_review(job, {})
    assert job["episodes"]["S01E01"]["state"] == "review"


def test_apply_review_acao_invalida(temp_db):
    job = _review_job()
    with pytest.raises(ValueError, match="inválida"):
        pipeline._apply_review(job, {"actions": {"S01E01": {"1": "explodir"}}})


@pytest.mark.ffmpeg
def test_render_corta_original_fundido_na_janela(tmp_path):
    """Original de 60 s = dois "episódios" de 30 s; a EDL cobre só o segundo
    (b_window 30–60). A saída tem ~30 s, o áudio dublado entra alinhado ao
    trecho cortado e os capítulos do original são deslocados/recortados
    para a janela (o do 1º episódio some)."""
    orig = _media(tmp_path / "orig.mkv", 60, seed=1,
                  chapters=[(0, 30, "Ep1"), (30, 45, "Ep2 A"), (45, 60, "Ep2 B")])
    dub = _media(tmp_path / "dub.mkv", 30, seed=2)
    segs = [Segment("match", 0.0, 30.0, 30.0, 60.0, offset=30.0)]
    out = tmp_path / "out.mkv"
    info_r = render_mod.render(segs, str(dub), str(orig), str(out), "pt",
                               log=lambda m: None, b_window=(30.0, 60.0))
    info = _probe(out)
    dur = float(info["format"]["duration"])
    # keyframe anterior ao início pode adiantar alguns segundos: 30 ≤ dur ≤ 42
    # — e NUNCA o arquivo inteiro (60 s): o corte da janela tem que valer
    assert 29.0 <= dur <= 42.0, dur
    assert abs(dur - (60.0 - info_r["b_shift"])) < 1.0, (dur, info_r)
    audios = [s for s in info["streams"] if s["codec_type"] == "audio"]
    assert len(audios) == 2
    shift = info_r["b_shift"]
    assert 0.0 < shift <= 30.0
    titles = [c.get("tags", {}).get("title", "") for c in info["chapters"]]
    assert "Ep2 A" in titles and "Ep2 B" in titles, titles
    ch = {c["tags"]["title"]: float(c["start_time"]) for c in info["chapters"]}
    assert abs(ch["Ep2 B"] - (45.0 - shift)) < 0.05, (ch, shift)


@pytest.mark.ffmpeg
def test_render_janela_no_comeco_corta_o_fim(tmp_path):
    """Caso real S02E01: janela 0–30 de um original de 60 s. O -to de entrada
    do ffmpeg NÃO era honrado (saía o arquivo inteiro); a saída tem que ter
    ~30 s, e o vídeo do 2º episódio fica de fora."""
    orig = _media(tmp_path / "orig.mkv", 60, seed=1,
                  chapters=[(0, 30, "Ep1"), (30, 60, "Ep2")])
    dub = _media(tmp_path / "dub.mkv", 30, seed=2)
    segs = [Segment("match", 0.0, 30.0, 0.0, 30.0, offset=0.0)]
    out = tmp_path / "out.mkv"
    render_mod.render(segs, str(dub), str(orig), str(out), "pt",
                      log=lambda m: None, b_window=(0.0, 30.0))
    info = _probe(out)
    dur = float(info["format"]["duration"])
    assert 29.0 <= dur <= 31.5, dur
    # capítulo do 2º episódio: some, ou fica vazio colado no fim (start == -t,
    # que o ffmpeg mantém por "≤") — nunca com conteúdo dentro da saída
    for c in info["chapters"]:
        if c["tags"]["title"] == "Ep2":
            assert float(c["start_time"]) >= 29.5, c
    assert info["chapters"][0]["tags"]["title"] == "Ep1"
