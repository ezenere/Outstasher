"""Validação do offset em janelas espaçadas (merger.alignment_windows /
classify_offsets / measure_offsets) — sem ffmpeg: os offsets das janelas são
dados, o que se testa é ONDE se mede e o que se decide com o que foi medido.
O caminho com áudio real está em test_align_ffmpeg.py."""
import pytest

from services import merger
from services.series import subs

W = merger.ALIGN_DURATION
GOOD, WEAK = 80.0, 7.5   # qualidade de pico: correlação real x ruído puro


def _windows(duration, offset_at, q_at=lambda t: GOOD):
    """Janelas do plano real para `duration`, com o offset (s) e a qualidade
    que as funções dão para o CENTRO de cada janela."""
    return [(t, offset_at(t + W / 2), q_at(t + W / 2))
            for t in merger.alignment_windows(duration)]


def _old_windows(duration):
    """Onde o merge ANTIGO media (duas janelas de 300 s: 0:30 e ~60%)."""
    start2 = min(max(30.0 + 300.0, duration * 0.6), duration - 300.0 - 30)
    return [(30.0, 330.0), (start2, start2 + 300.0)]


# -------------------- onde medir --------------------

@pytest.mark.parametrize("minutes", [40, 45, 90, 120, 150, 180])
def test_janelas_cobrem_o_filme_a_cada_15_20_min(minutes):
    d = minutes * 60.0
    starts = merger.alignment_windows(d)
    # a primeira pula a abertura; a última termina antes dos créditos
    assert starts[0] == merger.ALIGN_START
    assert starts[-1] + W <= 0.85 * d + 1e-6
    assert starts[-1] + W >= 0.85 * d - 1.0      # ... mas vai até lá
    gaps = [b - a for a, b in zip(starts, starts[1:])]
    assert all(15 * 60 <= g <= 20 * 60 for g in gaps), gaps


def test_filme_de_3h_tem_9_janelas_e_2h_tem_7():
    assert len(merger.alignment_windows(3 * 3600.0)) == 9
    assert len(merger.alignment_windows(2 * 3600.0)) == 7


def test_arquivo_curto_mede_uma_janela_so():
    assert merger.alignment_windows(200.0) == [merger.ALIGN_START]
    # menor que ALIGN_START + janela: mede do começo (antes: WAV vazio a 30 s)
    assert merger.alignment_windows(6.0) == [0.0]
    # duração desconhecida (probe sem duração): o comportamento antigo
    assert merger.alignment_windows(0.0) == [merger.ALIGN_START]


# -------------------- o que decidir --------------------

def test_offset_constante_em_n_janelas_usa_a_mediana():
    jitter = iter([0.0, 0.0011, -0.0007, 0.0004, 0.0, 0.0009, -0.0002])
    wins = _windows(7200.0, lambda t: 1.250 + next(jitter))
    chk = merger.classify_offsets(wins)
    assert chk.verdict == "constant" and len(chk.good) == 7
    assert chk.offset == pytest.approx(1.250, abs=1e-6)  # a mediana, não a média
    assert chk.rate == 0.0 and not chk.diverge


def test_degrau_depois_das_janelas_antigas_vira_corte():
    """Caso que as duas janelas antigas deixavam passar: a versão dublada
    perdeu 400 ms aos 83 min de um filme de 2 h — depois da janela dos 60%
    (72-77 min). As duas antigas viam 0 ms e "validavam"; com uma janela a
    cada ~17 min as duas últimas veem -400 ms: degrau."""
    d, cut_at = 7200.0, 5000.0
    assert all(b < cut_at for _, b in _old_windows(d))   # antigas: antes do corte
    chk = merger.classify_offsets(_windows(d, lambda t: 0.0 if t < cut_at else -0.400))
    assert chk.verdict == "cut"
    assert chk.tau1_ms == 0.0 and chk.tau2_ms == -400.0
    assert chk.offset == 0.0       # merge forçado: o "offset do início"


def test_cena_trocada_no_meio_vira_corte_mesmo_com_as_pontas_concordando():
    """Entre 25 e 60 min o dublado tem uma cena 0,5 s mais longa e depois
    uma 0,5 s mais curta: início e fim concordam (as duas janelas antigas,
    em 0:30 e 72 min, também), mas o meio do filme sairia meio segundo fora."""
    chk = merger.classify_offsets(
        _windows(7200.0, lambda t: 0.5 if 1500 < t < 3600 else 0.0))
    assert chk.verdict == "cut"
    assert chk.tau2_ms == 500.0


def test_drift_linear_e_reconhecido_com_inclinacao_e_intercepto():
    rate = 5e-5    # 180 ms/h: velocidade levemente diferente
    chk = merger.classify_offsets(_windows(7200.0, lambda t: 0.200 + rate * t))
    assert chk.verdict == "drift"
    assert chk.rate == pytest.approx(rate, rel=1e-6)
    assert chk.offset == pytest.approx(0.200, abs=1e-6)  # intercepto em t=0


def test_reta_ingreme_demais_nao_e_drift():
    """3e-4 (1 s/h) não sai de um GCC-PHAT em janela de 60 s — a deriva
    dentro da janela apagaria o pico. Uma "reta" assim é escada de cortes."""
    chk = merger.classify_offsets(_windows(7200.0, lambda t: 3e-4 * t))
    assert chk.verdict == "cut"


def test_tres_janelas_nao_bastam_para_chamar_de_drift():
    wins = _windows(45 * 60.0, lambda t: 1e-4 * t)
    assert len(wins) == 3
    assert merger.classify_offsets(wins).verdict == "cut"


def test_janela_de_silencio_e_ignorada():
    """Silêncio/música sem correlação no meio do filme: o pico fica no nível
    do ruído e o 'offset' é lixo — não pode decidir nada."""
    def off(t):
        return 33.4 if 2000 < t < 2200 else 0.120

    def q(t):
        return WEAK if 2000 < t < 2200 else GOOD
    wins = _windows(7200.0, off, q)
    assert sum(1 for w in wins if w[2] < merger.ALIGN_MIN_QUALITY) == 1
    chk = merger.classify_offsets(wins)
    assert chk.verdict == "constant" and len(chk.good) == 6
    assert chk.offset == pytest.approx(0.120)


def test_cena_longa_a_mais_nao_vira_offset_constante():
    """Versão estendida com 3 min a mais aos 50 min: dali em diante o offset
    passa da busca (±60 s) e TODAS as janelas ficam sem correlação. Descartar
    e seguir daria 'constante' com as três primeiras — e meio filme fora."""
    wins = _windows(7200.0, lambda t: 0.0 if t < 3000 else 17.3,
                    lambda t: GOOD if t < 3000 else WEAK)
    chk = merger.classify_offsets(wins)
    assert chk.verdict == "unknown"
    assert "última" in chk.reason
    assert chk.tau1_ms == chk.tau2_ms == 0.0   # não há "segundo offset"


@pytest.mark.parametrize("weak_idx, what", [(0, "primeira"), (-1, "última")])
def test_ponta_sem_correlacao_nao_confirma(weak_idx, what):
    wins = _windows(7200.0, lambda t: 0.0)
    t, o, _ = wins[weak_idx]
    wins[weak_idx] = (t, o, WEAK)
    chk = merger.classify_offsets(wins)
    assert chk.verdict == "unknown" and what in chk.reason


def test_poucas_janelas_boas_nao_confirmam():
    wins = _windows(7200.0, lambda t: 0.0)
    wins = [w if i in (0, len(wins) - 1) else (w[0], w[1], WEAK)
            for i, w in enumerate(wins)]
    chk = merger.classify_offsets(wins)
    assert chk.verdict == "unknown" and "só 2 de 7" in chk.reason


def test_episodio_curto_com_duas_janelas_exige_as_duas():
    wins = _windows(22 * 60.0, lambda t: 0.3)
    assert len(wins) == 2
    assert merger.classify_offsets(wins).verdict == "constant"
    wins[1] = (wins[1][0], wins[1][1], WEAK)
    assert merger.classify_offsets(wins).verdict == "unknown"


# -------------------- medição (retry nas pontas) --------------------

def test_ponta_fraca_e_medida_de_novo_uma_janela_para_dentro(monkeypatch):
    d = 7200.0
    starts = merger.alignment_windows(d)
    calls = []

    def fake(ref, ra, oth, oa, start):
        calls.append(start)
        # a última posição cai num trecho silencioso; 60 s antes há som
        return (0.050, WEAK) if start == starts[-1] else (0.050, GOOD)
    monkeypatch.setattr(merger, "_measure_window", fake)
    logs = []
    wins = merger.measure_offsets("r.mkv", 0, "o.mkv", 0, d, log=logs.append)
    assert calls[-1] == starts[-1] - W
    assert wins[-1] == (starts[-1] - W, 0.050, GOOD)
    assert merger.classify_offsets(wins).verdict == "constant"
    assert any("medindo de novo" in m for m in logs)
    assert sum("Offset na janela" in m for m in logs) == len(starts)


def test_erro_do_ffmpeg_em_todas_as_janelas_sobe(monkeypatch):
    def broken(*a, **k):
        raise merger.MergeError("falha ao extrair áudio")
    monkeypatch.setattr(merger, "_measure_window", broken)
    with pytest.raises(merger.MergeError, match="falha ao extrair"):
        merger.measure_offsets("r.mkv", 0, "o.mkv", 0, 7200.0, log=lambda m: None)


def test_erro_numa_janela_so_conta_como_janela_fraca(monkeypatch):
    starts = merger.alignment_windows(7200.0)

    def flaky(ref, ra, oth, oa, start):
        if start == starts[3]:
            raise merger.MergeError("pacote corrompido")
        return 0.0, GOOD
    monkeypatch.setattr(merger, "_measure_window", flaky)
    wins = merger.measure_offsets("r.mkv", 0, "o.mkv", 0, 7200.0, log=lambda m: None)
    assert wins[3][2] == 0.0
    assert merger.classify_offsets(wins).verdict == "constant"


# -------------------- VersionMismatch e consumidores --------------------

def test_version_mismatch_compativel_com_os_dois_offsets():
    e = merger.VersionMismatch(-100.0, 700.0)
    assert (e.tau1_ms, e.tau2_ms, e.verdict, e.windows) == (-100.0, 700.0, "cut", [])
    assert e.summary == "offsets divergentes (-100 → +700 ms)"
    assert "possível corte/versão diferente" in str(e)


def test_version_mismatch_nao_confirmado_explica_o_motivo():
    e = merger.VersionMismatch(0.0, 0.0, [(30.0, 0.0, 80.0), (4000.0, 9.1, 7.0)],
                               verdict="unknown", reason="a última janela não tem correlação confiável")
    assert e.summary.startswith("offset não confirmado (a última janela")
    assert len(e.windows) == 2


def test_cadeia_do_audio_com_drift_ajusta_a_velocidade():
    chain = merger.build_audio_fix_chain("[1:a:0]", 0.250, 5e-5)
    assert chain == ("[1:a:0]aresample=async=1:first_pts=0,asetpts=N/SR/TB,"
                     "atrim=start=0.250000,asetpts=N/SR/TB,"
                     "asetpts=PTS/1.000050000,aresample=async=50")
    # sem drift: exatamente a cadeia de antes
    assert merger.build_audio_fix_chain("[1:a:0]", -0.2) == (
        "[1:a:0]aresample=async=1:first_pts=0,asetpts=N/SR/TB,adelay=200:all=1")
    assert merger.build_audio_fix_chain("[1:a:0]", 0.0) == (
        "[1:a:0]aresample=async=1:first_pts=0,asetpts=N/SR/TB")


def test_legenda_externa_acompanha_o_drift_corrigido():
    # outro arquivo: conteúdo de t (referência) em 0.2 + 1.0001·t; o merge o
    # acelera e devolve input_shifts/input_rates — a legenda volta para t
    rate, tau = 1e-4, 0.2
    fn = subs.shift_fn(-tau / (1 + rate), 1 + rate)
    for t in (0.0, 600.0, 5400.0):
        assert fn(tau + (1 + rate) * t) == pytest.approx(t, abs=1e-9)
    assert subs.shift_fn(1.5)(10.0) == 11.5   # sem rate: o deslocamento de sempre
