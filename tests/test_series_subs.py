"""Legendas externas do torrent: localizar, idioma, remapear, deduplicar, muxar."""
import json
import subprocess

import pytest

from services.series import subs


def _srt(path, cues):
    subs.write_srt(cues, path)
    return path


# -------------------- localizar --------------------

def test_find_por_referencia_no_nome_e_na_pasta(tmp_path):
    root = tmp_path / "Show.S01.1080p"
    (root / "Subs" / "S01E02").mkdir(parents=True)
    (root / "Show.S01E01.1080p.mkv").write_bytes(b"x")
    (root / "Show.S01E02.1080p.mkv").write_bytes(b"x")
    (root / "Show.S01E01.1080p.pt-BR.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\noi\n")
    (root / "Subs" / "S01E02" / "2_English.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nhi\n")
    (root / "Subs" / "S01E02" / "3_Portuguese.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\noi\n")
    (root / "sample.srt").write_text("x")
    e1 = subs.find_for_episode(root, 1, 1, str(root / "Show.S01E01.1080p.mkv"))
    assert [p.name for p in e1] == ["Show.S01E01.1080p.pt-BR.srt"]
    e2 = subs.find_for_episode(root, 1, 2, str(root / "Show.S01E02.1080p.mkv"))
    assert sorted(p.name for p in e2) == ["2_English.srt", "3_Portuguese.srt"]


def test_find_torrent_de_um_video_leva_todas(tmp_path):
    root = tmp_path / "Show.S01E05.WEB"
    root.mkdir()
    (root / "Show.S01E05.WEB.mkv").write_bytes(b"x")
    (root / "English.srt").write_text("x")
    (root / "Portugues.ass").write_text("x")
    (root / "notes.txt").write_text("x")
    found = subs.find_for_episode(root, 1, 5, str(root / "Show.S01E05.WEB.mkv"))
    assert sorted(p.name for p in found) == ["English.srt", "Portugues.ass"]


# -------------------- idioma / sabor --------------------

@pytest.mark.parametrize("name,lang", [
    ("Show.S01E01.pt-BR.srt", "por"),
    ("Show.S01E01.por.srt", "por"),
    ("2_English.srt", "eng"),
    ("Show.S01E01.eng.forced.srt", "eng"),
    ("Español (Latino).srt", "spa"),
])
def test_guess_language_pelo_nome(tmp_path, name, lang):
    p = tmp_path / name
    p.write_text("x")
    assert subs.guess_language(p, "Show.S01E01.mkv") == lang


def test_guess_language_pela_pasta_e_pelo_conteudo(tmp_path):
    d = tmp_path / "Subs" / "pt-BR"
    d.mkdir(parents=True)
    p = d / "3.srt"
    p.write_text("x")
    assert subs.guess_language(p) == "por"
    q = tmp_path / "7.srt"
    q.write_text("\n".join(
        f"{i}\n00:00:0{i % 9},000 --> 00:00:0{i % 9},500\nI don't know what you "
        f"want, but that is not the thing you have.\n" for i in range(12)))
    assert subs.guess_language(q) == "eng"
    r = tmp_path / "8.srt"
    r.write_text("1\n00:00:01,000 --> 00:00:02,000\nx\n")
    assert subs.guess_language(r, fallback="por") == "por"


def test_flavor():
    assert subs.flavor_of("Show.S01E01.eng.forced.srt", "Show.S01E01.mkv") == "forced"
    assert subs.flavor_of("English (SDH).srt") == "sdh"
    assert subs.flavor_of("Show.S01E01.pt.srt", "Show.S01E01.mkv") == "normal"


# -------------------- ler / remapear --------------------

def test_parse_srt_cp1252_e_remap(tmp_path):
    p = tmp_path / "a.srt"
    p.write_bytes("1\n00:00:10,000 --> 00:00:12,000\nNão é você\n\n"
                  "2\n00:00:20,500 --> 00:00:22,000\nSegunda\n".encode("cp1252"))
    cues = subs.load_cues(p)
    assert cues == [(10.0, 12.0, "Não é você"), (20.5, 22.0, "Segunda")]
    shifted = subs.remap(cues, subs.shift_fn(1.5))
    assert shifted[0][:2] == (11.5, 13.5)
    # início fora (None) some; fim fora mantém a duração
    fn = lambda t: None if t < 15 else (t + 3 if t < 21 else None)  # noqa: E731
    out = subs.remap(cues, fn)
    assert out == [(23.5, 25.0, "Segunda")]


def test_edl_fn_segue_os_cortes_do_dublado():
    segments = [
        {"kind": "match", "a_start": 0.0, "a_end": 100.0, "b_start": 0.0,
         "b_end": 100.0, "offset": 0.0},
        {"kind": "gap_dub", "a_start": 100.0, "a_end": 130.0, "b_start": 100.0,
         "b_end": 100.0},
        {"kind": "match", "a_start": 130.0, "a_end": 300.0, "b_start": 100.0,
         "b_end": 270.0, "offset": -30.0},
        {"kind": "replaced", "a_start": 300.0, "a_end": 310.0, "b_start": 270.0,
         "b_end": 275.0},
    ]
    fn = subs.edl_fn(segments)
    assert fn(50.0) == 50.0
    assert fn(115.0) is None            # dentro do trecho descartado
    assert fn(200.0) == 170.0           # depois do corte: -30 s
    assert fn(305.0) is None            # substituída sem use_dub: some
    segments[3]["action"] = "use_dub"
    assert subs.edl_fn(segments)(305.0) == pytest.approx(272.5)
    # janela do original fundido: tudo desloca
    assert subs.edl_fn(segments, b_shift=10.0)(200.0) == 160.0


# -------------------- deduplicar --------------------

def test_plan_dedup_contra_embutida_e_entre_lados(tmp_path):
    o1 = tmp_path / "Show.S01E01.pt-BR.srt"; o1.write_text("x")
    o2 = tmp_path / "Show.S01E01.eng.srt"; o2.write_text("x")
    d1 = tmp_path / "Dub.S01E01.pt.srt"; d1.write_text("x")
    d2 = tmp_path / "Dub.S01E01.pt.forced.srt"; d2.write_text("x")
    d3 = tmp_path / "Dub.S01E01.spa.srt"; d3.write_text("x")
    items, skipped = subs.plan(
        [o1, o2], [d1, d2, d3], embedded={("eng", "normal")},
        orig_video="Show.S01E01.mkv", dub_video="Dub.S01E01.mkv")
    got = [(i["side"], i["lang"], i["flavor"]) for i in items]
    # eng externa do original: já embutida (texto) → fora
    # pt do dublado: duplica a pt do original → fora; forçada pt e spa entram
    assert got == [("orig", "por", "normal"), ("dub", "por", "forced"),
                   ("dub", "spa", "normal")], got
    assert len(skipped) == 2


def test_embedded_text_keys_ignora_bitmap():
    probe = {"streams": [
        {"codec_type": "subtitle", "codec_name": "hdmv_pgs_subtitle",
         "tags": {"language": "eng"}},
        {"codec_type": "subtitle", "codec_name": "subrip",
         "tags": {"language": "pob"}, "disposition": {"forced": 1}},
    ]}
    assert subs.embedded_text_keys(probe) == {("por", "forced")}


# -------------------- muxar (ffmpeg de verdade) --------------------

def _mkv(path, dur=20, sub=None):
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           "-f", "lavfi", "-i", f"testsrc2=s=160x90:d={dur}:r=24",
           "-f", "lavfi", "-i", f"anoisesrc=d={dur}"]
    if sub:
        cmd += ["-i", str(sub)]
    cmd += ["-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-preset",
            "ultrafast", "-c:a", "ac3", "-metadata:s:a:0", "language=eng"]
    if sub:
        cmd += ["-map", "2:0", "-c:s", "srt", "-metadata:s:s:0", "language=eng"]
    cmd += [str(path)]
    subprocess.run(cmd, check=True)
    return path


def _probe(path):
    p = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json",
                        "-show_streams", "-show_chapters", str(path)],
                       capture_output=True, text=True, check=True)
    return json.loads(p.stdout)


@pytest.mark.ffmpeg
def test_attach_muxa_remapeia_e_deduplica(tmp_path):
    emb = _srt(tmp_path / "emb.srt", [(1.0, 2.0, "embedded en")])
    out = _mkv(tmp_path / "out.mkv", sub=emb)
    o_en = _srt(tmp_path / "Show.S01E01.eng.srt", [(1.0, 2.0, "dup en")])
    d_pt = _srt(tmp_path / "Dub.S01E01.pt-BR.srt", [(5.0, 6.0, "oi"), (12.0, 13.0, "tchau")])
    n = subs.attach(str(out), [o_en], [d_pt], subs.shift_fn(0.0),
                    subs.shift_fn(2.0), "Show.S01E01.mkv", "Dub.S01E01.mkv",
                    log=lambda m: None)
    assert n == 1
    info = _probe(out)
    st = [s for s in info["streams"] if s["codec_type"] == "subtitle"]
    assert [s["tags"]["language"] for s in st] == ["eng", "por"]
    # a legenda pt entrou deslocada +2 s
    p = subprocess.run(["ffmpeg", "-v", "error", "-i", str(out), "-map", "0:s:1",
                        "-f", "srt", "-"], capture_output=True, text=True, check=True)
    assert "00:00:07,000 --> 00:00:08,000" in p.stdout
    assert "00:00:14,000 --> 00:00:15,000" in p.stdout
    # vídeo/áudio continuam lá
    assert [s["codec_type"] for s in info["streams"]][:2] == ["video", "audio"]
