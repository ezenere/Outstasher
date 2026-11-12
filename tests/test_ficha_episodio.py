"""Ficha do episódio e timeline ao vivo do alinhamento/render.

- o render devolve o PLANO (fatias com o motivo, cortes do vídeo na posição
  em que caem na saída) — é a linha "Saída" da timeline;
- o refino avisa cada etapa (a timeline ao vivo atualiza entre elas);
- relatório por episódio no banco (método, janelas, plano), log recortado
  por episódio (marca data.ep e, em job antigo, pela posição no log);
- a timeline ao vivo só reenvia a lista quando muda (rev global);
- a ficha junta tudo e sonda as faixas de cada arquivo.
"""
import shutil
import subprocess

import pytest

from services.jobs import details, runtime
from services.series.align import refine, render
from services.series.align.classify import Segment


# -------------------- plano do render --------------------

def test_fatias_dizem_o_porque():
    segs = [
        Segment("match", 0.0, 100.0, 0.0, 100.0, offset=0.0),
        Segment("gap_orig", 100.0, 100.0, 100.0, 130.0, note="intervalo"),
        Segment("match", 100.0, 200.0, 130.0, 230.0, offset=30.0),
    ]
    segs[0].extra["refine"] = "offset medido +12.0 ms (áudio no lugar)"
    segs[1].extra["action"] = "silence"
    segs[1].extra["picture_s"] = 0.1
    slices, _ = render._plan_slices(segs, 230.0)
    assert [s["src"] for s in slices] == ["dub", "silence", "dub"]
    assert slices[0]["why"] == "trecho casado — offset medido +12.0 ms (áudio no lugar)"
    assert slices[1]["why"] == "cena sem dublagem — tela preta — silêncio (decisão) — intervalo"
    assert slices[1]["kind"] == "gap_orig"


def test_plano_com_cortes_na_posicao_da_saida():
    slices = [
        {"src": "dub", "src_start": 0.0, "src_end": 50.0, "b_start": 0.0, "b_end": 50.0,
         "kind": "match", "why": "trecho casado"},
        {"src": "dub", "src_start": 40.0, "src_end": 90.0, "b_start": 50.0, "b_end": 100.0,
         "kind": "match", "why": "trecho casado", "stretch": 0.2},
    ]
    # dois cortes no ORIGINAL: o segundo cai na saída 10 s antes (o primeiro
    # já tirou 10 s)
    cuts = [(20.0, 30.0), (60.0, 65.0)]
    alvos = [(20.0, 30.0, "gap_orig", 0.0), (60.0, 65.0, "replaced", None)]
    plan = render._render_plan(slices, cuts, alvos, 100.0, None)
    assert plan["duration"] == 100.0
    assert plan["slices"][1]["offset"] == 10.0
    assert plan["slices"][1]["stretch"] == 0.2
    assert [c["at"] for c in plan["cuts"]] == [20.0, 50.0]
    assert plan["cuts"][0]["why"] == "cena sem dublagem (tela preta) — cortada do vídeo"
    assert plan["cuts"][1]["why"] == "cena substituída — cortada do vídeo"
    assert plan["window"] is None


# -------------------- refino avisa as etapas --------------------

def test_refino_avisa_cada_etapa(monkeypatch):
    ident = lambda segs, *a, **k: segs  # noqa: E731
    for nome in ("collapse_wobbles", "_resolve_replaced_by_audio", "_reclaim_gap_dub",
                 "_tighten_extra_dub", "_refine_cut_junctions", "_snap_gap_edges"):
        monkeypatch.setattr(refine, nome, ident)
    monkeypatch.setattr(refine, "_drop_stray_matches", lambda out, log, *a, **k: out)
    monkeypatch.setattr(refine, "_drop_spurious_gaps", lambda out, log: out)
    monkeypatch.setattr(refine, "_profile", lambda *a, **k: [])
    monkeypatch.setattr(refine, "_split_by_profile", lambda seg, *a, **k: [seg])
    segs = [Segment("match", 0.0, 300.0, 0.0, 300.0, offset=0.0),
            Segment("gap_orig", 300.0, 300.0, 300.0, 310.0),
            Segment("match", 300.0, 600.0, 310.0, 610.0, offset=10.0)]
    vistos = []
    refine.refine_offsets(segs, "d", 0, "o", 0, log=lambda m: None,
                          on_stage=lambda lista, r: vistos.append((r, len(lista))))
    rotulos = [r for r, _ in vistos]
    assert "Medindo o offset pelo áudio (1/2)" in rotulos
    assert "Medindo o offset pelo áudio (2/2)" in rotulos
    assert rotulos[-1] == "Junções de cena refinadas"
    # no meio da medição a lista está inteira (medidos + o que falta)
    assert all(n == 3 for _, n in vistos)


def test_timeline_quebrada_nao_derruba_o_refino(monkeypatch):
    monkeypatch.setattr(refine, "collapse_wobbles", lambda segs, *a, **k: segs)
    monkeypatch.setattr(refine, "_resolve_replaced_by_audio", lambda segs, *a, **k: segs)
    monkeypatch.setattr(refine, "_reclaim_gap_dub", lambda segs, *a, **k: segs)

    def explode(*_):
        raise RuntimeError("ui")
    out = refine.refine_offsets([Segment("gap_dub", 0.0, 5.0, None, None)],
                                "d", 0, "o", 0, log=lambda m: None, on_stage=explode)
    assert len(out) == 1


# -------------------- timeline ao vivo --------------------

def test_timeline_so_reenvia_quando_muda():
    job = {"id": "tl1"}
    runtime._timelines.pop("tl1", None)
    runtime.timeline_scan(job, "S01E01", {"label": "dublado", "pct": 40.0})
    v = runtime.timeline_view("tl1")
    assert v["scan"] == {"dub": 40.0} and v["segments"] is None
    rev = v["rev"]
    assert runtime.timeline_view("tl1", rev) == {"key": "S01E01", "rev": rev, "unchanged": True}
    runtime.timeline_segments(job, "S01E01", [Segment("match", 0, 10, 0, 10, offset=0.0)],
                              "Casamento pelas imagens", 10.0, 10.0)
    v2 = runtime.timeline_view("tl1", rev)
    assert not v2.get("unchanged") and v2["segments"][0]["kind"] == "match"
    assert v2["scan"] == {"dub": 100.0, "orig": 100.0}
    # episódio seguinte: timeline nova, e o rev nunca repete o anterior
    runtime.timeline_scan(job, "S01E02", {"label": "original", "pct": 5.0})
    v3 = runtime.timeline_view("tl1", v2["rev"])
    assert v3["key"] == "S01E02" and v3["rev"] > v2["rev"] and v3["segments"] is None
    runtime._end_episode(job)
    assert runtime.timeline_view("tl1") is None


# -------------------- relatório e log do episódio --------------------

def _job_row(store, jid="fx1"):
    store.upsert_job({
        "id": jid, "tmdb_id": 1, "language": "pt", "mode": "auto", "kind": "both",
        "status": "merging", "detail": "", "output": None,
        "movie": {"original_title": "Serie Exemplo", "year": "2020"},
        "progress": {"video": None, "audio": None, "merge": None},
        "created_at": "2026-01-01T00:00:00", "search_queries": [], "search_candidates": [],
    })


def test_relatorio_junta_e_reseta(temp_db):
    _job_row(temp_db)
    job = {"id": "fx1"}
    runtime._report(job, "S01E01", reset=True, started_at="x")
    runtime._report(job, "S01E01", method="edl", plan={"duration": 1.0})
    runtime._report(job, "S01E01", error=None, result="done")
    assert temp_db.load_report("fx1", "S01E01") == {
        "started_at": "x", "method": "edl", "plan": {"duration": 1.0}, "result": "done"}
    runtime._report(job, "S01E01", reset=True, method="offset")
    assert temp_db.load_report("fx1", "S01E01") == {"method": "offset"}
    runtime._report(job, None, method="hardlink")      # filme: chave ""
    assert temp_db.load_report("fx1", "") == {"method": "hardlink"}


def test_relatorio_sem_banco_nao_derruba(monkeypatch):
    from services import store
    monkeypatch.setattr(store, "update_report",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("sem banco")))
    runtime._report({"id": "x"}, "S01E01", method="edl")   # não levanta


def test_log_do_episodio_pela_marca(temp_db):
    _job_row(temp_db)
    job = {"id": "fx1"}
    # _add_event é onde a marca entra (o _event só agenda a gravação do job)
    runtime._add_event(job, "merge", "antes de tudo")
    runtime._begin_episode(job, "S01E01")
    runtime._add_event(job, "merge", "S01E01: fazendo merge")
    runtime._add_event(job, "merge", "  match 0-100s")
    runtime._end_episode(job)
    runtime._begin_episode(job, "S01E02")
    runtime._add_event(job, "merge", "S01E02: fazendo merge")
    runtime._end_episode(job)
    msgs = [e["message"] for e in temp_db.load_episode_events("fx1", "S01E01")]
    assert msgs == ["S01E01: fazendo merge", "  match 0-100s"]
    ev = temp_db.load_episode_events("fx1", "S01E02")[0]
    assert ev["data"] == {"ep": "S01E02"}


def test_log_de_job_antigo_pela_posicao(temp_db):
    """Job de antes da marca: o trecho entre o fechamento do episódio
    anterior e o deste (o merge de série é um episódio por vez)."""
    _job_row(temp_db)
    for m in ["Convertendo 3 episódio(s) (um por vez)...",
              "S01E01: offset constante", "✅ S01E01 concluído: /x/e1.mkv",
              "S01E02: offset varia — rodando o alinhador por conteúdo...",
              "  match 0-100s dividido em 2 parte(s)",
              "Gravando o arquivo final...",
              "✅ S01E02 concluído (EDL): /x/e2.mkv",
              "S01E03: fazendo merge", "  wobble de 12s"]:
        temp_db.add_event("fx1", {"ts": "2026-01-01T00:00:00", "kind": "merge", "message": m})
    msgs = [e["message"] for e in temp_db.load_episode_events("fx1", "S01E02")]
    assert msgs[0].startswith("S01E02: offset varia") and msgs[-1].startswith("✅ S01E02")
    assert len(msgs) == 4
    # em curso (sem fechamento): do último fechamento até agora
    em_curso = [e["message"] for e in temp_db.load_episode_events("fx1", "S01E03")]
    assert em_curso == ["S01E03: fazendo merge", "  wobble de 12s"]
    # nunca processado: nada
    assert temp_db.load_episode_events("fx1", "S01E09") == []


# -------------------- a ficha --------------------

def test_ficha_do_episodio(temp_db):
    _job_row(temp_db, "fx2")
    job = {"id": "fx2", "media_type": "tv", "status": "merging",
           "episodes": {"S01E01": {
               "season": 1, "episode": 1, "name": "Piloto", "state": "done",
               "error": None, "output": "/dest/e1.mkv",
               "src": {"original": "/t/o.mkv", "dubbed": "/t/d.mkv"},
               "subs": {"original": ["/t/o.srt"]}, "edl": None}},
           "torrents": [{"n": 0, "role": "video", "title": "Serie Exemplo S01 1080p",
                         "coverage": ["S01E01", "S01E02"], "state": "done", "size": 5},
                        {"n": 1, "role": "audio", "title": "Serie Exemplo S01 Dublado",
                         "coverage": ["S01E02"], "state": "done", "size": 3}]}
    runtime._jobs["fx2"] = job
    try:
        runtime._report(job, "S01E01", method="offset", offset_ms=12.5)
        d = details.episode("fx2", "S01E01")
        assert d["files"] == {"original": "/t/o.mkv", "dubbed": "/t/d.mkv",
                              "file": None, "output": "/dest/e1.mkv"}
        assert [t["n"] for t in d["torrents"]] == [0]
        assert d["report"]["offset_ms"] == 12.5
        assert details.episode("fx2", "S09E09") is None
        assert details.movie("fx2") is None      # é série
    finally:
        runtime._jobs.pop("fx2", None)


def test_sonda_arquivo_ausente_e_real(tmp_path):
    out = details.probe_paths({"original": str(tmp_path / "sumiu.mkv"), "dubbed": None})
    assert out == {"original": {"path": str(tmp_path / "sumiu.mkv"), "exists": False}}
    if not shutil.which("ffmpeg"):
        pytest.skip("sem ffmpeg")
    f = tmp_path / "a.mkv"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i",
                    "testsrc=size=64x48:rate=24:duration=1", "-f", "lavfi", "-i",
                    "sine=duration=1", "-metadata:s:a:0", "language=por",
                    "-c:v", "mpeg4", "-c:a", "aac", str(f)], check=True)
    p = details.probe_paths({"output": str(f)})["output"]
    assert p["exists"] and p["size"] > 0 and p["duration"] == pytest.approx(1.0, abs=0.2)
    tipos = {s["type"]: s for s in p["streams"]}
    assert tipos["video"]["width"] == 64 and tipos["video"]["fps"] == 24.0
    assert tipos["audio"]["lang"] == "por" and tipos["audio"]["channels"] == 1


@pytest.mark.ffmpeg
def test_render_publica_e_devolve_o_plano(tmp_path):
    """Render de verdade: o plano sai ANTES do ffmpeg (on_plan, a timeline ao
    vivo) e volta no resultado (o relatório do episódio)."""
    from tests.test_series_render import _media
    orig = _media(tmp_path / "orig.mkv", 30, seed=1)
    dub = _media(tmp_path / "dub.mkv", 25, seed=2)
    segs = [Segment("match", 0.0, 15.0, 0.0, 15.0, offset=0.0),
            Segment("gap_orig", 15.0, 15.0, 15.0, 20.0, note="cena extra"),
            Segment("match", 15.0, 25.0, 20.0, 30.0, offset=5.0)]
    publicados = []
    info = render.render(segs, str(dub), str(orig), str(tmp_path / "out.mkv"), "pt",
                         log=lambda m: None, original_lang="en",
                         on_plan=publicados.append)
    assert publicados and publicados[0] == info["plan"]
    plan = info["plan"]
    assert [s["src"] for s in plan["slices"]][:3] == ["dub", "orig", "dub"]
    assert plan["slices"][1]["why"].startswith("cena sem dublagem")
    assert plan["slices"][2]["offset"] == 5.0
    assert plan["cuts"] == []
