"""DP de alinhamento (gap afim, semi-global) sobre sequências sintéticas.

Sequências de hashes construídas à mão com respostas CONHECIDAS: identidade,
deleção, inserção, substituição e as pontas livres. O kernel numba roda via
align_reference (banda cobrindo a matriz inteira) — o mesmo código de
produção, sem cópia paralela.
"""
import numpy as np

from services.series.align import classify, dp, fingerprint
from services.series.align.fingerprint import FPS, hamming_band


def _hashes(n: int, seed: int = 1) -> np.ndarray:
    """n hashes distintos (Hamming esperado entre dois ~32, bem acima do
    limiar de match)."""
    rng = np.random.default_rng(seed)
    return rng.integers(0, 2 ** 63, size=n, dtype=np.uint64)


def _kinds(runs):
    return [r[0] for r in runs]


def test_identicas_um_match_unico():
    a = _hashes(120)
    runs = dp.align_reference(a, a.copy())
    assert runs == [(dp.MATCH, 0, 120, 0, 120)]


def test_delecao_no_original_vira_gap_a():
    # B = A sem o trecho [40:70): esses 30 frames existem SÓ no dublado
    a = _hashes(120)
    b = np.concatenate([a[:40], a[70:]])
    runs = dp.align_reference(a, b)
    assert runs == [
        (dp.MATCH, 0, 40, 0, 40),
        (dp.GAP_A, 40, 70, 40, 40),
        (dp.MATCH, 70, 120, 40, 90),
    ]


def test_insercao_no_original_vira_gap_b():
    # B tem 30 frames extras no meio (cena que a dublagem não tem)
    b = _hashes(120)
    a = np.concatenate([b[:40], b[70:]])
    runs = dp.align_reference(a, b)
    assert runs == [
        (dp.MATCH, 0, 40, 0, 40),
        (dp.GAP_B, 40, 40, 40, 70),
        (dp.MATCH, 40, 90, 70, 120),
    ]


def test_gap_afim_prefere_um_gap_longo():
    # deleções espalhadas NÃO viram 30 gaps de 1 frame — o afim consolida;
    # aqui uma deleção contígua tem que sair como UM run, não fragmentos
    a = _hashes(200)
    b = np.concatenate([a[:100], a[160:]])
    runs = dp.align_reference(a, b)
    gaps = [r for r in runs if r[0] == dp.GAP_A]
    assert len(gaps) == 1
    assert gaps[0] == (dp.GAP_A, 100, 160, 100, 100)


def test_pontas_livres_recap_e_creditos():
    # dublado tem 20 frames de recap no começo; original tem 15 de créditos
    # extras no fim — os dois saem como gaps de ponta, sem penalizar o meio
    core = _hashes(100)
    a = np.concatenate([_hashes(20, seed=2), core])
    b = np.concatenate([core, _hashes(15, seed=3)])
    runs = dp.align_reference(a, b)
    assert runs == [
        (dp.GAP_A, 0, 20, 0, 0),
        (dp.MATCH, 20, 120, 0, 100),
        (dp.GAP_B, 120, 120, 100, 115),
    ]


def test_banda_reproduz_matriz_cheia():
    a = _hashes(150)
    b = np.concatenate([a[:50], a[80:]])
    full = dp.align_reference(a, b)
    D, lo = hamming_band(a, b, band=60)  # banda apertada mas suficiente
    banded = dp.align_band(D, lo, len(b))
    assert banded == full


def test_sem_match_algum_retorna_sem_match():
    # arquivos de episódios DIFERENTES: nada casa — sintoma de ordem trocada;
    # o chamador converte em conflito (gate), não em alinhamento forçado
    a = _hashes(80, seed=10)
    b = _hashes(80, seed=20)
    runs = dp.align_reference(a, b)
    assert not any(r[0] == dp.MATCH and r[2] - r[1] > 3 for r in runs)


# -------------------- classificação --------------------

def _classify(a, b, band=None):
    if band is None:
        band = max(len(a), len(b))
    D, lo = hamming_band(a, b, band=band)
    runs = dp.align_band(D, lo, len(b))
    return classify.classify_runs(runs, D, lo)


def test_classifica_match_e_gap():
    a = _hashes(240)
    b = np.concatenate([a[:80], a[140:]])  # 60 frames (15 s) só no dublado
    segs = _classify(a, b)
    kinds = [s.kind for s in segs]
    assert kinds == ["match", "gap_dub", "match"]
    gap = segs[1]
    assert abs((gap.a_end - gap.a_start) - 60 / FPS) < 1.0
    # offsets dos matches: 0 antes do corte; -15 s depois (b atrasado)
    assert abs(segs[0].offset - 0.0) < 0.3
    assert abs(segs[2].offset - (-60 / FPS)) < 0.3


def test_classifica_substituicao_por_par_de_gaps():
    # mesmo tamanho, conteúdo diferente no meio: o DP contorna com um gap de
    # cada lado e o pós-processamento funde no segmento `replaced`
    a = _hashes(200)
    b = a.copy()
    b[80:120] = _hashes(40, seed=9)
    segs = _classify(a, b)
    kinds = [s.kind for s in segs]
    assert "replaced" in kinds, kinds
    rep = next(s for s in segs if s.kind == "replaced")
    assert abs((rep.a_end - rep.a_start) - 40 / FPS) < 1.5
    assert classify.needs_review(segs) == [rep]


def test_gap_minusculo_e_descartado():
    # 1 frame divergente = ruído de hash, não edição: não pode virar segmento
    a = _hashes(100)
    b = a.copy()
    b[50] = np.uint64(0)
    segs = _classify(a, b)
    assert all(s.kind == "match" for s in segs), [s.kind for s in segs]


def test_perfil_de_confianca():
    a = _hashes(240)
    b = np.concatenate([a[:80], a[140:]])
    segs = _classify(a, b)
    prof = classify.confidence_profile(segs, duration_a=60.0)
    assert len(prof) == 60
    # dentro do match a confiança é alta; dentro do gap, média (é decisão)
    assert prof[5] > 0.9
    assert prof[25] == 0.5  # 80/4=20 s .. 140/4=35 s é o gap


def test_check_duration_ratio():
    assert classify.check_duration_ratio(1320.0, 1290.0) is None
    assert classify.check_duration_ratio(2640.0, 1320.0) is not None  # fundido
    assert classify.check_duration_ratio(0.0, 100.0) is not None


def test_plano_parado_ruidoso_nao_vira_substituicao():
    """Falso positivo real: plano parado onde as duas encodes divergem um
    pouco (resíduo ~16, acima do limiar de match 12) — o DP contorna com dois
    gaps e o pós-processamento fundia em 'replaced'. Com o miolo MEDIDO igual,
    volta a ser match (+ o gap pequeno da diferença de duração)."""
    a = _hashes(200)
    b = a.copy()
    # ruído moderado nos frames 80..120: inverte 16 bits fixos de cada hash
    mask = np.uint64((1 << 16) - 1)
    b[80:120] = a[80:120] ^ mask
    segs = _classify(a, b)
    kinds = [s.kind for s in segs]
    assert "replaced" not in kinds, kinds
    # e o trecho ruidoso é atravessado como match (offset 0)
    for s in segs:
        if s.kind == "match":
            assert abs(s.offset) < 0.3


def test_coarse_offset_localiza_episodio_no_fundido():
    """Episódio curto dentro de um arquivo longo (fundido): o localizador
    grosseiro acha o offset dominante mesmo com um RECAP no começo do curto
    (cenas de outro ponto do longo, que votam em outra diagonal)."""
    ep = _hashes(1200, seed=1)                  # 5 min a 4 fps
    other = _hashes(1000, seed=2)               # o outro episódio (antes)
    tail = _hashes(200, seed=3)
    fused = np.concatenate([other, ep, tail])
    # curto = recap (60 frames do OUTRO episódio) + o episódio
    short = np.concatenate([other[400:460], ep])
    off, frac = fingerprint.coarse_offset(short, fused)
    # o episódio começa em 1000 no fundido e em 60 no curto: offset ~940
    assert abs(off - 940) <= fingerprint.COARSE_STEP * 2, (off, frac)
    assert frac > 0.5


def test_par_de_gaps_solto_na_ponta_nao_e_substituicao():
    """Par gap_dub+gap_orig sem trecho casado de um dos lados (começo do
    arquivo, rabo do outro episódio) fica como gaps — não vira 'replaced'
    pedindo revisão."""
    a = _hashes(200)
    b = np.concatenate([_hashes(40, seed=7), a[40:]])   # começo diferente
    segs = _classify(a, b)
    kinds = [s.kind for s in segs]
    assert "replaced" not in kinds, kinds
    assert kinds[-1] == "match"
