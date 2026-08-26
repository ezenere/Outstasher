"""Refino por ÁUDIO: perfil de offset, corte em silêncio, wobbles e
'substituída' resolvida pelo áudio — com áudio REAL (ffmpeg).

Reproduz um caso real de campo (S01E01, WEB-DL BR vs BluRay): o dublado tem uma edição de 2 frames numa
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


def test_substituida_com_vizinho_na_grade_do_video(tmp_path):
    """Caso real de campo (S01E03 aos 14:20): o vizinho diz offset -5,00 (grade de
    0,25 s do vídeo) mas o áudio real está em -5,14. A 'substituída' tem que
    ser comparada com o offset REAL do vizinho, não com o da grade — senão os
    41 ms de sobra estouram a tolerância e o falso positivo vai para revisão."""
    orig = _audio(tmp_path / "orig.wav", 60, seed=5, gaps=[(20, 20.4), (40, 40.4)])
    # dublado = original ATRASADO 140 ms (offset real = -0.14: b = a - 0.14)
    dub = tmp_path / "dub.wav"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-i", str(orig), "-af", "adelay=140:all=1",
                    "-c:a", "pcm_s16le", str(dub)], check=True)
    segs = [
        Segment("match", 0.14, 25.0, 0.0, 24.86, offset=0.0),      # vídeo: grade
        Segment("replaced", 25.0, 30.0, 25.0, 30.0),
        Segment("match", 30.0, 59.0, 30.0, 59.0, offset=0.0),
    ]
    logs = []
    out = refine.refine_offsets(segs, str(dub), 0, str(orig), 0, log=logs.append)
    assert all(s.kind == "match" for s in out), [(s.kind, s.a_start) for s in out]
    assert any("vira match" in l for l in logs), logs
    assert not any("fica para revisão" in l for l in logs), logs


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
    # o gap fica no EDL (descreve o que saiu do dublado), com b colapsado
    assert [s.kind for s in out] == ["match", "gap_dub", "match"]
    a, g, b = out
    assert g.b_start == g.b_end == a.b_end
    assert abs(a.a_end - 30.1) < 0.4          # corte no silêncio 29.8-30.4
    assert abs(b.a_start - (a.a_end + 0.6)) < 1e-6  # pula o excesso do dublado
    assert abs(b.b_start - a.b_end) < 1e-6    # original contínuo
    assert any("junção" in l for l in logs)


def test_descarte_da_juncao_cabe_inteiro_no_silencio(tmp_path):
    """Caso real (E05 aos 31:10): 905 ms a mais no dublado (preto do intervalo
    comercial) e silêncio de ~1,1 s. Mandar o CORTE para o meio do silêncio
    fazia a janela de descarte [corte, corte+extra] sair pela borda direita e
    comer ~360 ms audíveis da cena seguinte. A janela inteira tem que caber
    no silêncio."""
    dub = _audio(tmp_path / "dub.wav", 70, seed=9, gaps=[(29.9, 31.0)])
    segs = [
        Segment("match", 0.0, 30.0, 0.0, 30.0, offset=0.0),
        Segment("gap_dub", 30.0, 30.9, 30.0, 30.0),      # 0,9 s só no dublado
        Segment("match", 30.9, 60.0, 30.0, 59.1, offset=-0.9),
    ]
    logs = []
    out = refine._tighten_extra_dub(segs, str(dub), 0, log=logs.append)
    a, g, b = out
    # descarte = [a.a_end, a.a_end + 0.9] dentro do silêncio 29.9-31.0
    assert 29.9 - 0.05 <= a.a_end, a.a_end
    assert a.a_end + 0.9 <= 31.0 + 0.05, a.a_end
    assert abs(b.a_start - (a.a_end + 0.9)) < 1e-6
    assert abs(b.b_start - a.b_end) < 1e-6    # original contínuo
    assert any("descarte" in l for l in logs), logs


def _dub_with_scene_cut(orig_wav, path, cut_at, drop):
    """'Dublado' = o áudio original com [cut_at, cut_at+drop) REMOVIDO — a
    cena existe só no original (censura/edição de TV)."""
    return _dub_with_edit(orig_wav, path, cut_at=cut_at, drop=drop)


def test_juncao_de_cena_cortada_sai_da_grade_do_video(tmp_path):
    """[match][gap_orig][match]: a fronteira vinha crua do vídeo (grade de
    0,25 s — aqui, de propósito, 2,2 s fora). O áudio bissecta a junção e o
    corte cai no silêncio ao lado do ponto real; os lados b seguem os offsets
    medidos, então o preenchimento se ajusta sozinho."""
    orig = _audio(tmp_path / "orig.wav", 60, seed=11,
                  gaps=[(29.9, 30.2), (40.2, 40.5), (12.0, 12.3), (50.0, 50.3)])
    dub = _dub_with_scene_cut(orig, tmp_path / "dub.wav", cut_at=30.2, drop=10.0)
    segs = [
        Segment("match", 0.0, 28.0, 0.0, 28.0, offset=0.0),
        Segment("gap_orig", 28.0, 28.0, 28.0, 38.0),
        Segment("match", 28.0, 49.5, 38.0, 59.5, offset=10.0),
    ]
    logs = []
    out = refine.refine_offsets(segs, str(dub), 0, str(orig), 0, log=logs.append)
    matches = [x for x in out if x.kind == "match"]
    gaps = [x for x in out if x.kind == "gap_orig" and (x.b_end - x.b_start) > 1]
    assert len(matches) == 2 and len(gaps) == 1, [(x.kind, x.a_start, x.a_end) for x in out]
    a, g, b = matches[0], gaps[0], matches[1]
    # o corte saiu de 28,0 (vídeo) para o silêncio junto do ponto real (30,2)
    assert 29.8 <= a.a_end <= 30.6, a.a_end
    assert b.a_start == a.a_end
    # lados b derivados dos offsets MEDIDOS: preenchimento de ~10 s
    assert abs(a.b_end - (a.a_end + a.offset)) < 1e-6
    assert abs(b.b_start - (b.a_start + b.offset)) < 1e-6
    assert (g.b_start, g.b_end) == (a.b_end, b.b_start)
    assert 9.5 <= g.b_end - g.b_start <= 10.5, (g.b_start, g.b_end)
    assert any("junção de cena cortada" in l for l in logs), logs


def test_bordas_de_recap_no_meio_encaixam_no_silencio(tmp_path):
    """[match][gap_dub grande][match] (recap no meio): o corte sai da grade do
    vídeo para o silêncio do dub e o ORIGINAL fica contínuo — o recap inteiro
    é pulado dentro do silêncio, sem buraco nem sobreposição em b."""
    orig = _audio(tmp_path / "orig.wav", 40, seed=21,
                  gaps=[(19.8, 20.15), (8.0, 8.3), (33.0, 33.3)])
    recap = _audio(tmp_path / "recap.wav", 15, seed=99,
                   gaps=[(0.0, 0.25), (14.75, 15.0)])
    dub = tmp_path / "dub.wav"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-i", str(orig), "-i", str(recap),
         "-filter_complex",
         "[0:a]atrim=0:20,asetpts=PTS-STARTPTS[p1];"
         "[0:a]atrim=20,asetpts=PTS-STARTPTS[p2];"
         "[p1][1:a][p2]concat=n=3:v=0:a=1[a]",
         "-map", "[a]", "-c:a", "pcm_s16le", str(dub)], check=True)
    segs = [
        Segment("match", 0.0, 20.25, 0.0, 20.25, offset=0.0),
        Segment("gap_dub", 20.25, 34.75, 20.25, 20.25),
        Segment("match", 34.75, 54.75, 19.75, 39.75, offset=-15.0),
    ]
    out = refine.refine_offsets(segs, str(dub), 0, str(orig), 0,
                                log=lambda m: None)
    a = next(x for x in out if x.kind == "match" and x.a_start < 5)
    g = next(x for x in out if x.kind == "gap_dub")
    b = next(x for x in out if x.kind == "match" and x.a_start > 25)
    # o corte saiu do 20,25 do vídeo para o silêncio [19,8-20,15]
    assert 19.8 <= a.a_end <= 20.2, a.a_end
    # e o outro lado retoma exatamente depois do recap (15 s de dublado)
    assert abs(b.a_start - (a.a_end + 15.0)) < 0.3, (a.a_end, b.a_start)
    # ORIGINAL contínuo: sem buraco (preenchido com inglês) nem sobreposição
    assert abs(b.b_start - a.b_end) < 1e-6, (a.b_end, b.b_start)
    assert abs(a.b_end - (a.a_end + a.offset)) < 1e-6
    # o gap continua no EDL, descrevendo o que foi descartado do dublado
    assert g.a_start == a.a_end and abs(g.a_end - b.a_start) < 1e-6
    assert g.b_start == g.b_end == a.b_end


def test_replaced_confere_o_vizinho_de_DEPOIS_tambem(tmp_path):
    """Caso real de campo (5 episódios seguidos mandados para revisão): há uma
    junção DENTRO do trecho suspeito, então o offset muda de um lado para o
    outro (+0,00 antes, -0,30 depois) e o áudio do trecho é contínuo com o de
    DEPOIS. Comparar só com o vizinho anterior recusava por ~300 ms — a
    diferença exata entre os dois vizinhos."""
    orig = _audio(tmp_path / "orig.wav", 90, seed=31,
                  gaps=[(29.7, 30.1), (33.0, 33.4), (60.0, 60.4)])
    # dublado = original com 300 ms REMOVIDOS aos 30 s: antes offset 0,
    # depois o dublado adianta 0,3 s (b = a + 0,3)
    dub = _dub_with_edit(orig, tmp_path / "dub.wav", cut_at=30.0, drop=0.300)
    segs = [
        Segment("match", 0.0, 29.0, 0.0, 29.0, offset=0.0),
        # o vídeo marcou como "substituída" a janela que contém a junção
        Segment("replaced", 29.0, 32.0, 29.0, 32.0, residual=22.0),
        Segment("match", 32.0, 89.0, 32.3, 89.3, offset=0.3),
    ]
    logs = []
    out = refine._resolve_replaced_by_audio(segs, str(dub), 0, str(orig), 0,
                                            log=logs.append)
    assert [s.kind for s in out] == ["match", "match", "match"], logs
    resolvido = out[1]
    # ficou com o offset do vizinho de DEPOIS (a junção está antes do trecho)
    assert abs(resolvido.offset - 0.3) < 0.05, (resolvido.offset, logs)
    assert any("vira match" in l for l in logs), logs


def test_replaced_curto_ainda_e_medido(tmp_path):
    """Trecho de 0,97 s (caso real) tinha áudio casando a 0,2 ms e ia para
    revisão sem nem ser medido — a guarda era 1,0 s. Quem barra medição ruim
    é o pico, não a duração."""
    orig = _audio(tmp_path / "orig.wav", 60, seed=33)
    same = tmp_path / "dub.wav"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-i", str(orig), "-c:a", "pcm_s16le", str(same)], check=True)
    segs = [
        Segment("match", 0.0, 29.0, 0.0, 29.0, offset=0.0),
        Segment("replaced", 29.0, 29.97, 29.0, 29.97, residual=29.0),
        Segment("match", 29.97, 59.0, 29.97, 59.0, offset=0.0),
    ]
    logs = []
    out = refine._resolve_replaced_by_audio(segs, str(same), 0, str(orig), 0,
                                            log=logs.append)
    assert out[1].kind == "match", logs


def test_gap_dub_que_casa_com_o_vizinho_e_recuperado(tmp_path):
    """Caso real (5,4 s rotulados gap_dub casando com pico 982 no offset do
    match seguinte): o DP pôs a fronteira cedo demais. Dublado = original sem
    [25, 34): a verdade é dub 0-25 ↔ 0-25 e dub 25-50 ↔ 34-59. O vídeo
    entregou o gap_dub 25-30 + gap_orig 25-39 + match a 30-50. O áudio tem
    que devolver os 5 s ao match seguinte e encolher o buraco para 25-34."""
    orig = _audio(tmp_path / "orig.wav", 60, seed=41)
    dub = _dub_with_edit(orig, tmp_path / "dub.wav", cut_at=25.0, drop=9.0)
    segs = [
        Segment("match", 0.0, 25.0, 0.0, 25.0, offset=0.0),
        Segment("gap_dub", 25.0, 30.0, 25.0, 25.0),
        Segment("gap_orig", 30.0, 30.0, 25.0, 39.0),
        Segment("match", 30.0, 50.0, 39.0, 59.0, offset=9.0),
    ]
    logs = []
    out = refine._reclaim_gap_dub(segs, str(dub), 0, str(orig), 0, log=logs.append)
    assert [s.kind for s in out] == ["match", "gap_orig", "match"], (out, logs)
    a, g, b = out
    assert abs(b.a_start - 25.0) < 1e-6 and abs(b.b_start - 34.0) < 1e-6
    assert abs(g.b_start - 25.0) < 1e-6 and abs(g.b_end - 34.0) < 0.05
    assert any("recuperada" in l for l in logs), logs


def test_gap_dub_que_casa_com_o_anterior_e_recuperado(tmp_path):
    """Espelho: o gap_dub é a CAUDA do match anterior (dub 25-30 ↔ orig
    25-30, offset 0); o buraco real do original começa só em 30."""
    orig = _audio(tmp_path / "orig.wav", 60, seed=43)
    dub = _dub_with_edit(orig, tmp_path / "dub.wav", cut_at=30.0, drop=9.0)
    segs = [
        Segment("match", 0.0, 25.0, 0.0, 25.0, offset=0.0),
        Segment("gap_dub", 25.0, 30.0, 25.0, 25.0),
        Segment("gap_orig", 30.0, 30.0, 25.0, 39.0),
        Segment("match", 30.0, 50.0, 39.0, 59.0, offset=9.0),
    ]
    out = refine._reclaim_gap_dub(segs, str(dub), 0, str(orig), 0, log=lambda m: None)
    assert [s.kind for s in out] == ["match", "gap_orig", "match"], out
    a, g, b = out
    assert abs(a.a_end - 30.0) < 1e-6 and abs(a.b_end - 30.0) < 1e-6
    assert abs(g.b_start - 30.0) < 0.05 and abs(g.b_end - 39.0) < 1e-6


def test_gap_dub_de_verdade_fica(tmp_path):
    """gap_dub cujo áudio NÃO casa com nenhum vizinho (recap: conteúdo de
    outro lugar) continua gap_dub."""
    orig = _audio(tmp_path / "orig.wav", 60, seed=45)
    recap = _audio(tmp_path / "recap.wav", 6, seed=99)
    dub = tmp_path / "dub.wav"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-i", str(orig), "-i", str(recap), "-filter_complex",
                    "[0:a]atrim=0:25,asetpts=PTS-STARTPTS[p1];"
                    "[0:a]atrim=25,asetpts=PTS-STARTPTS[p2];"
                    "[p1][1:a][p2]concat=n=3:v=0:a=1[a]",
                    "-map", "[a]", "-c:a", "pcm_s16le", str(dub)], check=True)
    segs = [
        Segment("match", 0.0, 25.0, 0.0, 25.0, offset=0.0),
        Segment("gap_dub", 25.0, 31.0, 25.0, 25.0),
        Segment("match", 31.0, 60.0, 25.0, 54.0, offset=-6.0),
    ]
    out = refine._reclaim_gap_dub(segs, str(dub), 0, str(orig), 0, log=lambda m: None)
    assert [s.kind for s in out] == ["match", "gap_dub", "match"], out


def test_juncao_guarda_ponto_exato_alem_do_encaixe_em_silencio(tmp_path):
    """A junção de cena cortada guarda `junction_a` (posição CRUA, precisa)
    além da fronteira encaixada no silêncio: o cut_video corta ali, onde o
    dublado pula — o silêncio pode estar segundos longe (ligação contínua)."""
    orig = _audio(tmp_path / "orig.wav", 60, seed=61,
                  gaps=[(12.0, 12.3), (50.0, 50.3)])   # silêncios LONGE do corte
    dub = _dub_with_scene_cut(orig, tmp_path / "dub.wav", cut_at=30.2, drop=10.0)
    segs = [
        Segment("match", 0.0, 28.0, 0.0, 28.0, offset=0.0),
        Segment("gap_orig", 28.0, 28.0, 28.0, 38.0),
        Segment("match", 28.0, 49.5, 38.0, 59.5, offset=10.0),
    ]
    out = refine.refine_offsets(segs, str(dub), 0, str(orig), 0, log=lambda m: None)
    g = next(x for x in out if x.kind == "gap_orig" and (x.b_end - x.b_start) > 1)
    ja = g.extra.get("junction_a")
    assert ja is not None, out
    assert abs(ja - 30.2) < 0.4, ja        # bissecção fina: ~0,2 s do corte real
