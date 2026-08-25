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
            # keyframe a cada 2 s, como release de streaming: cut_video corta
            # em keyframes e o GOP default do x264 (~10 s) não daria onde
            "-g", "48", "-keyint_min", "48",
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
    # ffmpeg 7.x passa do -t em até um GOP no stream copy (o 9.x corta seco):
    # a folga cobre as duas gerações
    assert -1.0 < dur - (60.0 - info_r["b_shift"]) < 2.5, (dur, info_r)
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
    # a duração DECLARADA importa tanto quanto a real: capítulo além do fim
    # inflava o cabeçalho (player mostrava 81 min num episódio de 40 — o
    # recorte de capítulos pelo -t varia entre versões do ffmpeg, então os
    # capítulos da janela entram por ffmetadata, nunca por -map_chapters 0)
    assert 29.0 <= dur <= 31.5, dur
    titles = [c["tags"]["title"] for c in info["chapters"]]
    assert titles == ["Ep1"], titles


def _srt(path, cues):
    path.write_text("\n".join(
        f"{i}\n{a}\n{t}\n" for i, (a, t) in enumerate(cues, 1)), encoding="utf-8")
    return path


@pytest.mark.ffmpeg
def test_render_leva_todas_as_legendas(tmp_path):
    """Legenda comum é o motivo de existir legenda: a saída leva TODAS as
    faixas do original, inclusive as de línguas sem áudio na saída."""
    orig = _media(tmp_path / "orig.mkv", 20, seed=1)
    dub = _media(tmp_path / "dub.mkv", 20, seed=2)
    langs = ("eng", "por", "kor", "tha")
    files = {lg: _srt(tmp_path / f"{lg}.srt",
                      [("00:00:01,000 --> 00:00:02,000", lg)]) for lg in langs}
    with_subs = tmp_path / "orig_subs.mkv"
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(orig)]
    for f in files.values():
        cmd += ["-i", str(f)]
    cmd += ["-map", "0"]
    for k in range(len(files)):
        cmd += ["-map", f"{k + 1}:0"]
    cmd += ["-c", "copy"]
    for k, lg in enumerate(langs):
        cmd += [f"-metadata:s:s:{k}", f"language={lg}"]
    cmd += [str(with_subs)]
    subprocess.run(cmd, check=True)

    out = tmp_path / "out.mkv"
    render_mod.render([Segment("match", 0.0, 20.0, 0.0, 20.0, offset=0.0)],
                      str(dub), str(with_subs), str(out), "pt", log=lambda m: None)
    got = [s.get("tags", {}).get("language")
           for s in _probe(out)["streams"] if s["codec_type"] == "subtitle"]
    assert sorted(got) == sorted(langs), got


def test_intercalacao_dimensionada_pelo_orcamento():
    """O delta de intercalação vem de um ORÇAMENTO de bytes: num 1080p sai
    enorme (na prática, estrito); num REMUX 4K limita a memória do muxer por
    construção — em vez do 'estrito até estourar' que derrubou o servidor."""
    from services import merger as m
    # REMUX 4K: 85,6 Mb/s = 10,7 MB/s -> 1 GB segura ~100 s
    quatro_k = m.sized_interleave_delta(85.6e6 / 8)
    assert 60 <= quatro_k / 1e6 <= 180, quatro_k
    # o buffer implícito nunca passa do orçamento
    assert quatro_k / 1e6 * 85.6e6 / 8 <= m.MUX_BUFFER_GB * 1024 ** 3 * 1.01
    # 1080p a 10 Mb/s: ~14 min de tolerância (estrito, na prática)
    assert m.sized_interleave_delta(10e6 / 8) / 1e6 > 600
    # sem bitrate conhecido = arquivo pequeno: estrito
    assert m.sized_interleave_delta(0) == int(m.INTERLEAVE_MAX_S * 1e6)
    # e nunca abaixo do piso do ffmpeg
    assert m.sized_interleave_delta(1e12) == int(m.INTERLEAVE_MIN_S * 1e6)


def test_aviso_quando_o_muxer_forcou_saida():
    """'forcing output' no stderr = o arquivo saiu com intercalação frouxa —
    é isso que engasga players, então o usuário TEM que ficar sabendo."""
    from services import merger as m
    err = ("[matroska @ 0x1] Delay between the first packet and last packet "
           "in the muxing queue is 101000000 > 100000000: forcing output")
    warn = m.interleave_warning(err + "\n" + err)
    assert warn and "INTERCALAÇÃO FROUXA" in warn and "2x" in warn
    assert m.interleave_warning("") is None
    assert m.interleave_warning("tudo certo") is None


def test_mkvmerge_cmd_marca_dublagem_como_padrao():
    """O comando do mkvmerge: tudo do original entra como está, os áudios dele
    perdem a flag de padrão e a faixa dublada entra com o idioma alvo como
    padrão (ids de faixa = index do ffprobe)."""
    probe = {"streams": [
        {"index": 0, "codec_type": "video"},
        {"index": 1, "codec_type": "audio"},
        {"index": 2, "codec_type": "audio"},
        {"index": 3, "codec_type": "subtitle"},
    ]}
    cmd = render_mod._mkvmerge_cmd("out.mkv", "orig.mkv", "dub.mka", "por", probe)
    assert cmd[:3] == ["mkvmerge", "-o", "out.mkv"]
    assert cmd.count("--default-track-flag") == 3
    assert "1:no" in cmd and "2:no" in cmd and "0:yes" in cmd
    assert cmd[cmd.index("--language") + 1] == "0:por"
    # ordem: flags do original antes dele; as da dublada entre os dois inputs
    assert cmd.index("orig.mkv") < cmd.index("--language") < cmd.index("dub.mka")


def test_truncamento_silencioso_e_detectado(tmp_path):
    """Sob pressão de memória o ffmpeg JÁ saiu com código 0 e um arquivo pela
    metade: a duração da saída é conferida contra a esperada."""
    curto = _media(tmp_path / "curto.mkv", 5, seed=1)
    render_mod._check_mux_duration(str(curto), 5.0)          # ok
    with pytest.raises(render_mod.merger.MergeError, match="TRUNCADO"):
        render_mod._check_mux_duration(str(curto), 300.0)
    with pytest.raises(render_mod.merger.MergeError, match="ilegível"):
        render_mod._check_mux_duration(str(tmp_path / "nao_existe.mkv"), 5.0)


def test_erro_de_mux_sem_stderr_diz_que_foi_sinal():
    """Morto de fora (OOM killer) não deixa stderr: a mensagem tem que dizer
    isso, em vez do 'mux final falhou:' vazio que apareceu em produção."""
    from services import merger as m
    assert "sinal 9" in m.describe_exit(-9, "") and "memória" in m.describe_exit(-9, "")
    assert m.out_of_memory(-9, "")
    assert m.out_of_memory(1, "av_interleaved_write_frame: Cannot allocate memory")
    assert not m.out_of_memory(1, "Invalid data found")
    assert "Invalid data" in m.describe_exit(1, "Invalid data found")


def test_mux_aplica_o_teto_de_memoria_no_filho():
    """O teto tem que chegar ao PROCESSO do ffmpeg (é ele que estoura, não o
    servidor): o filho enxerga o limite; sem limite, fica ilimitado."""
    from services import merger as m
    # (o _run_mux lê o -progress do stdout, então o eco vai para o stderr)
    p = render_mod._run_mux(["sh", "-c", "ulimit -v 1>&2"])
    assert p.stderr.strip() == str(int(m.MUX_MEM_LIMIT_GB * 1024 ** 2))


@pytest.mark.ffmpeg
@pytest.mark.skipif(not render_mod.has_mkvmerge(), reason="sem mkvmerge no PATH")
def test_render_com_mkvmerge_de_verdade(tmp_path):
    """Com mkvmerge no PATH o mux final é dele: dublada como padrão no idioma
    alvo, áudios do original sem a flag, legendas e capítulos intactos."""
    orig = _media(tmp_path / "orig.mkv", 20, seed=1,
                  chapters=[(0, 10, "A"), (10, 20, "B")])
    dub = _media(tmp_path / "dub.mkv", 20, seed=2)
    out = tmp_path / "out.mkv"
    render_mod.render([Segment("match", 0.0, 20.0, 0.0, 20.0, offset=0.0)],
                      str(dub), str(orig), str(out), "pt", log=lambda m: None)
    info = _probe(out)
    audios = [s for s in info["streams"] if s["codec_type"] == "audio"]
    assert len(audios) == 2
    assert audios[1]["tags"]["language"] in ("por", "pt")
    assert audios[1]["disposition"]["default"] == 1
    assert audios[0]["disposition"]["default"] == 0
    assert [c["tags"]["title"] for c in info["chapters"]] == ["A", "B"]


@pytest.mark.ffmpeg
def test_mux_final_reporta_progresso(tmp_path, monkeypatch):
    """O mux do arquivo final é a etapa mais longa num REMUX (uma hora sem
    sinal, parecendo travado): ela tem que alimentar a barra até 100%."""
    orig = _media(tmp_path / "orig.mkv", 30, seed=1)
    dub = _media(tmp_path / "dub.mkv", 30, seed=2)
    vistos = []

    def on_progress(info):
        vistos.append(info["pct"])

    # sem mkvmerge: caminho do ffmpeg (-progress)
    monkeypatch.setattr(render_mod, "has_mkvmerge", lambda: False)
    render_mod.render([Segment("match", 0.0, 30.0, 0.0, 30.0, offset=0.0)],
                      str(dub), str(orig), str(tmp_path / "a.mkv"), "pt",
                      log=lambda m: None, on_progress=on_progress)
    assert vistos and vistos[-1] == 100.0
    assert vistos == sorted(vistos), vistos

    import shutil
    if shutil.which("mkvmerge") is None:
        return
    monkeypatch.undo()
    vistos.clear()
    render_mod.render([Segment("match", 0.0, 30.0, 0.0, 30.0, offset=0.0)],
                      str(dub), str(orig), str(tmp_path / "b.mkv"), "pt",
                      log=lambda m: None, on_progress=on_progress)
    assert vistos and vistos[-1] == 100.0


@pytest.mark.skipif(not render_mod.has_mkvmerge(), reason="sem mkvmerge no PATH")
def test_cut_video_remove_a_cena_sem_dublagem(tmp_path):
    """Ação cut_video (revisão): o original de 60 s tem uma cena [30-40] que
    não existe no dublado (50 s). Em vez de preencher com áudio original, a
    cena SAI do vídeo: a saída fica ~50 s, o corte cai em keyframe e o áudio
    dublado continua alinhado dos dois lados da junção."""
    orig = _media(tmp_path / "orig.mkv", 60, seed=1,
                  chapters=[(0, 30, "Antes"), (30, 40, "Extra"), (40, 60, "Depois")])
    dub = _media(tmp_path / "dub.mkv", 50, seed=2)
    segs = [
        Segment("match", 0.0, 30.0, 0.0, 30.0, offset=0.0),
        Segment("gap_orig", 30.0, 30.0, 30.0, 40.0,
                extra={"action": "cut_video"}),
        Segment("match", 30.0, 50.0, 40.0, 60.0, offset=10.0),
    ]
    out = tmp_path / "out.mkv"
    logs = []
    info = render_mod.render(segs, str(dub), str(orig), str(out), "pt",
                             log=logs.append)
    probe = _probe(out)
    dur = float(probe["format"]["duration"])
    # corte exato nos keyframes (30 e 40 s): 60 - 10 = 50, sem sobreposição
    assert abs(dur - 50.0) < 0.6, (dur, logs)
    assert any("cut_video" in l for l in logs), logs
    # a dublagem DEPOIS do corte tem que vir do lugar certo do dublado: o
    # trecho b=40-60 (dub a=30-50) virou b=30-50 na saída. Mede a faixa pt
    # da saída contra o dublado: aos 40 s da saída deve estar o dub de 40 s
    # (offset remapeado); sem o remapeio vinha o dub de 50 s
    from services.series.align import refine
    tau, q = refine._measure(str(out), 1, str(dub), 0, 40.0, 40.0, 6.0, radius=2.0)
    assert q > 20 and abs(tau) < 0.05, (tau, q, logs)
    # o mapa de cortes volta para quem for anexar legendas externas
    assert info.get("b_cuts"), info
    c0, c1 = info["b_cuts"][0]
    assert 29.0 <= c0 <= 31.5 and 38.5 <= c1 <= 40.5, info["b_cuts"]
    # capítulo da cena removida saiu junto (mkvmerge reajusta)
    titulos = [c.get("tags", {}).get("title") for c in probe.get("chapters", [])]
    assert "Extra" not in titulos, titulos


def test_cut_video_sem_mkvmerge_cai_no_preenchimento(tmp_path, monkeypatch):
    """Sem mkvmerge o corte não existe: o trecho fica no vídeo com áudio
    original (comportamento antigo), com aviso — nunca erro."""
    monkeypatch.setattr(render_mod, "has_mkvmerge", lambda: False)
    orig = _media(tmp_path / "orig.mkv", 30, seed=1)
    dub = _media(tmp_path / "dub.mkv", 20, seed=2)
    segs = [
        Segment("match", 0.0, 10.0, 0.0, 10.0, offset=0.0),
        Segment("gap_orig", 10.0, 10.0, 10.0, 20.0,
                extra={"action": "cut_video"}),
        Segment("match", 10.0, 20.0, 20.0, 30.0, offset=10.0),
    ]
    out = tmp_path / "out.mkv"
    logs = []
    render_mod.render(segs, str(dub), str(orig), str(out), "pt", log=logs.append)
    dur = float(_probe(out)["format"]["duration"])
    assert 29.0 <= dur <= 31.0, dur          # nada foi cortado
    assert any("não há mkvmerge" in l for l in logs), logs


def _srt_de(path, cues):
    path.write_text("\n".join(
        f"{i}\n{a} --> {b}\n{t}\n" for i, (a, b, t) in enumerate(cues, 1)),
        encoding="utf-8")
    return path


@pytest.mark.ffmpeg
def test_render_legendas_externas_no_mux_final(tmp_path):
    """As legendas externas entram no PRÓPRIO mux do render — sem a segunda
    reescrita do arquivo (num REMUX eram dezenas de GB de novo). A do lado
    dublado é remapeada pela EDL: com o dublado 10 s adiantado, a cue de
    00:05 tem que sair em 00:15 na timeline final."""
    orig = _media(tmp_path / "orig.mkv", 40, seed=1)
    dub = _media(tmp_path / "dub.mkv", 30, seed=2)
    sub_orig = _srt_de(tmp_path / "orig.eng.srt",
                       [("00:00:02,000", "00:00:04,000", "original line")])
    sub_dub = _srt_de(tmp_path / "dub.legenda.srt",
                      [("00:00:05,000", "00:00:07,000", "fala dublada")])
    # dublado 10 s adiantado: dub t=0 corresponde a orig t=10
    segs = [
        Segment("gap_orig", 0.0, 0.0, 0.0, 10.0),
        Segment("match", 0.0, 30.0, 10.0, 40.0, offset=10.0),
    ]
    out = tmp_path / "out.mkv"
    logs = []
    info = render_mod.render(
        segs, str(dub), str(orig), str(out), "pt", log=logs.append,
        external_subs={"orig": [str(sub_orig)], "dub": [str(sub_dub)],
                       "orig_lang": "eng", "dub_lang": "por"})
    assert info.get("subs_muxed") == 2, logs
    probe = _probe(out)
    subs = [s for s in probe["streams"] if s["codec_type"] == "subtitle"]
    langs = sorted((s.get("tags") or {}).get("language") for s in subs)
    assert langs == ["eng", "por"], langs
    assert any("mux final" in l for l in logs), logs


@pytest.mark.ffmpeg
def test_render_legenda_dublada_remapeada_pela_edl(tmp_path):
    """Só a legenda do dublado, com offset de +10 s na EDL: a cue de 5 s tem
    que aparecer aos ~15 s da saída (composição EDL feita DENTRO do render)."""
    orig = _media(tmp_path / "orig.mkv", 40, seed=1)
    dub = _media(tmp_path / "dub.mkv", 30, seed=2)
    sub_dub = _srt_de(tmp_path / "dub.legenda.srt",
                      [("00:00:05,000", "00:00:07,000", "fala dublada")])
    segs = [
        Segment("gap_orig", 0.0, 0.0, 0.0, 10.0),
        Segment("match", 0.0, 30.0, 10.0, 40.0, offset=10.0),
    ]
    out = tmp_path / "out.mkv"
    info = render_mod.render(
        segs, str(dub), str(orig), str(out), "pt", log=lambda m: None,
        external_subs={"dub": [str(sub_dub)], "dub_lang": "por"})
    assert info.get("subs_muxed") == 1
    p = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "s:0", "-show_packets",
         "-show_entries", "packet=pts_time", "-of", "csv=p=0", str(out)],
        capture_output=True, text=True)
    pts = [float(x.split(",")[0]) for x in p.stdout.split() if x.strip(",")]
    assert pts and abs(pts[0] - 15.0) < 0.2, (pts, p.stdout)
