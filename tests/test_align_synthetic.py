"""Alinhador de ponta a ponta com VÍDEO REAL (ffmpeg): estágios 0-3.

Gera pares de episódios sintéticos com divergências CONHECIDAS (re-encode
puro; corte no meio) e confere que a EDL descreve exatamente o que foi feito.
É a validação de campo do fingerprint + DP + classificação — nada stubado.
"""
import subprocess
import threading
import time

import pytest

from services.series.align import edl as edl_mod, engine, fingerprint

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
    # razão ~3: fora da faixa de "arquivo fundido" (~2) — segue conflito
    a = _base_video(tmp_path / "a.mkv", dur=20)
    b = _base_video(tmp_path / "b.mkv", dur=60)
    with pytest.raises(engine.AlignConflict, match="fundidos|divididos"):
        engine.align_pair(str(a), str(b))


def test_original_fundido_dois_episodios(tmp_path):
    """Caso real (razão ~2): o ORIGINAL é um arquivo com dois episódios
    concatenados; o dublado é só o segundo. Em vez de conflito, o alinhador
    localiza o episódio na 2ª metade e a EDL traz a janela do original."""
    ep1 = _base_video(tmp_path / "ep1.mkv", dur=40)
    # ep2 com conteúdo DIFERENTE (outra fonte lavfi) para as metades não se
    # confundirem
    ep2 = tmp_path / "ep2.mkv"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc=s=320x180:d=42:r=24",
         "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
         "-pix_fmt", "yuv420p", str(ep2)], check=True)
    fused = tmp_path / "fused.mkv"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-i", str(ep1), "-i", str(ep2), "-filter_complex",
         "[0:v][1:v]concat=n=2:v=1[v]", "-map", "[v]",
         "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
         "-pix_fmt", "yuv420p", str(fused)], check=True)
    dub = _reencoded(ep2, tmp_path / "dub.mkv")   # dublado = só o ep2

    edl = engine.align_pair(str(dub), str(fused), episode="S01E02")
    assert edl["merged_side"] == "orig"
    w0, w1 = edl["b_window"]
    # o episódio 2 começa em ~40 s no arquivo fundido (tolerância 4 s)
    assert abs(w0 - 40.0) < 4.0, edl["b_window"]
    assert w1 > 75.0
    st = edl_mod.stats(edl)
    assert st["match_pct"] > 90, edl["segments"]
    matches = [s for s in edl["segments"] if s["kind"] == "match"]
    assert matches and abs(matches[0]["offset"] - 40.0) < 2.0


def test_dublado_fundido_nao_precisa_de_janela_no_original(tmp_path):
    """Espelho: o DUBLADO é o fundido; o original é só o 1º episódio. Os
    tempos a saem absolutos e não há janela do original."""
    ep1 = _base_video(tmp_path / "ep1.mkv", dur=40)
    ep2 = tmp_path / "ep2.mkv"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc=s=320x180:d=42:r=24",
         "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
         "-pix_fmt", "yuv420p", str(ep2)], check=True)
    fused = tmp_path / "fused.mkv"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-i", str(ep1), "-i", str(ep2), "-filter_complex",
         "[0:v][1:v]concat=n=2:v=1[v]", "-map", "[v]",
         "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
         "-pix_fmt", "yuv420p", str(fused)], check=True)
    orig = _reencoded(ep1, tmp_path / "orig.mkv")
    edl = engine.align_pair(str(fused), str(orig), episode="S01E01")
    assert edl["merged_side"] == "dub"
    assert "b_window" not in edl and edl["a_window"][0] < 4.0
    matches = [s for s in edl["segments"] if s["kind"] == "match"]
    assert matches and abs(matches[0]["offset"]) < 2.0


def test_alinhamentos_nao_rodam_em_paralelo(tmp_path, monkeypatch):
    """Dois alinhamentos ao mesmo tempo brigam pelo disco: o segundo espera na
    fila. O fingerprint REAL roda nos dois; o teste só cronometra as janelas e
    confere que elas não se sobrepõem."""
    base = _base_video(tmp_path / "base.mkv", dur=20)
    other = _reencoded(base, tmp_path / "reenc.mkv")

    queued = threading.Event()          # o 2º avisou que entrou na fila
    spans, guard = [], threading.Lock()
    real = fingerprint.dhash_stream
    first = threading.Lock()

    def timed(*a, **kw):
        # o 1º a entrar segura o fingerprint até o 2º bater na fila: sem o
        # lock do alinhador, o 2º passaria direto e as janelas se sobreporiam
        held = first.acquire(blocking=False)
        if held:
            queued.wait(timeout=5)
        t0 = time.monotonic()
        try:
            return real(*a, **kw)
        finally:
            with guard:
                spans.append((t0, time.monotonic()))
            if held:
                first.release()

    monkeypatch.setattr(fingerprint, "dhash_stream", timed)

    waits = []

    def run():
        engine.align_pair(str(base), str(other),
                          on_wait=lambda: (waits.append(1), queued.set()))

    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=180)
        assert not t.is_alive(), "alinhamento travou"

    assert waits, "o segundo alinhamento não esperou na fila"
    spans.sort()
    for (_, fim), (comeco, _) in zip(spans, spans[1:]):
        assert fim <= comeco + 1e-6, f"fingerprints em paralelo: {spans}"

    # sem ninguém na frente, nada de fila
    sozinho = []
    engine.align_pair(str(base), str(other), on_wait=lambda: sozinho.append(1))
    assert sozinho == []


def test_progresso_do_fingerprint(tmp_path):
    """O fingerprint reporta progresso (o stdout é o dado, então a conta sai do
    volume de frames lido): um relatório por arquivo, terminando em 100%."""
    base = _base_video(tmp_path / "base.mkv", dur=30)
    other = _reencoded(base, tmp_path / "reenc.mkv")
    infos = []
    engine.align_pair(str(base), str(other), on_progress=infos.append)

    assert {i["step"] for i in infos} == {1, 2}
    assert {i["label"] for i in infos} == {"dublado", "original"}
    for step in (1, 2):
        ultimo = [i for i in infos if i["step"] == step][-1]
        assert ultimo["pct"] == 100.0, ultimo
        # 30 s de vídeo a 4 fps = 120 frames = 30 s de posição
        assert abs(ultimo["out_s"] - 30.0) < 1.0, ultimo
        assert abs(ultimo["duration_s"] - 30.0) < 1.0, ultimo
        assert ultimo["speed"] > 0 and ultimo["eta"] == 0
    # todo relatório é monotônico dentro do seu arquivo
    for step in (1, 2):
        pcts = [i["pct"] for i in infos if i["step"] == step]
        assert pcts == sorted(pcts), pcts


def test_crop_params_acha_barras_sujas(tmp_path):
    """Caso real de campo (REMUX 4K HDR): as barras do letterbox têm ruído
    acima do limiar padrão do cropdetect (24) e só aparecem com limiar >= 64.
    Sem isso, um lado era hasheado COM barras e o outro sem — distância ~14
    em cena idêntica e o DP desistia de blocos de 10+ min. O crop_params
    escala o limiar quando detecta "quadro inteiro", aceitando o resultado
    só se tiver formato de barras."""
    from services.series.align import fingerprint
    sujo = tmp_path / "sujo.mkv"
    # conteúdo 320x134 entre barras CINZA-ESCURO (luma ~40: "sujas")
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc2=size=320x134:rate=10:duration=40",
         "-vf", "pad=320:180:0:23:color=0x282828",
         "-c:v", "libx264", "-preset", "ultrafast", str(sujo)], check=True)
    crop = fingerprint.crop_params(str(sujo), duration=40.0)
    assert crop is not None
    w, h, x, y = (int(v) for v in crop.split(":"))
    assert w >= 310 and 128 <= h <= 140, crop     # largura cheia, altura do miolo
    assert 18 <= y <= 28, crop                    # barra de cima fora

    limpo = tmp_path / "limpo.mkv"
    # controle: conteúdo que ENCHE o quadro não pode ganhar crop nos limiares
    # altos (recorte irregular é descartado)
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=10:duration=40",
         "-c:v", "libx264", "-preset", "ultrafast", str(limpo)], check=True)
    crop2 = fingerprint.crop_params(str(limpo), duration=40.0)
    if crop2 is not None:
        w2, h2, x2, y2 = (int(v) for v in crop2.split(":"))
        assert w2 >= 310 and h2 >= 172, crop2     # continua quadro inteiro
