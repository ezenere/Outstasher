"""Estágio 3: classificação geométrica dos runs do DP.

A pergunta que a detecção de corte não responde: cada run vira um segmento
classificado pela GEOMETRIA do caminho + o RESÍDUO de distância dentro dele.

- diagonal, resíduo baixo   -> match (offset constante no trecho)
- salto só do dublado       -> gap_dub  (recap/abertura que o BD não tem)
- salto só do original      -> gap_orig (cena que a dublagem não tem)
- diagonal, resíduo ALTO    -> replaced (MESMA duração, conteúdo DIFERENTE —
                               assinatura inequívoca de cena substituída;
                               sempre revisão humana)
- inclinação ~1,0427        -> pal (speedup 25/23,976 — corrige com rubberband)
- inclinação constante != 1 -> drift (resample)

O pós-processamento é obrigatório: o DP fragmenta — sem fundir/descartar, o
revisor recebe 200 segmentos em vez de 6.
"""
from dataclasses import dataclass, field

import numpy as np

from services.series.align import dp as dp_mod
from services.series.align.fingerprint import FPS

RESIDUAL_MATCH = 12.0    # resíduo médio abaixo disto = mesmo conteúdo
RESIDUAL_REPLACED = 20.0  # acima disto na diagonal = conteúdo divergente
PAL_SLOPE = 25.0 / 23.976  # ~1.0427
SLOPE_TOL = 0.005
MIN_GAP_S = 0.5          # gap menor que isto é ruído de hash, não edição
MERGE_ADJACENT_S = 1.0   # runs do mesmo tipo separados por menos disto fundem
MERGE_OFFSET_FRAMES = 2  # matches vizinhos com offset ~igual fundem
# razão de duração que sugere episódios fundidos/divididos (detectar ANTES de
# alinhar: é problema de mapeamento de arquivo, não de alinhamento)
DURATION_RATIO_SUSPECT = 1.6


@dataclass
class Segment:
    """Trecho classificado do alinhamento (tempos em segundos).

    a_* = timeline do DUBLADO; b_* = timeline do ORIGINAL (o vídeo que fica).
    offset = b_start - a_start (o que somar ao tempo do dublado para achar o
    tempo correspondente no original), válido nos matches.
    """
    kind: str                    # match | gap_dub | gap_orig | replaced | pal | drift
    a_start: float
    a_end: float
    b_start: float | None
    b_end: float | None
    slope: float | None = None
    residual: float = 0.0
    confidence: float = 1.0
    offset: float | None = None
    note: str = ""
    extra: dict = field(default_factory=dict)


def check_duration_ratio(dur_a: float, dur_b: float) -> str | None:
    """Sinal de episódios fundidos/divididos — RODAR ANTES de alinhar."""
    if dur_a <= 0 or dur_b <= 0:
        return "duração inválida em um dos arquivos"
    ratio = max(dur_a, dur_b) / min(dur_a, dur_b)
    if ratio >= DURATION_RATIO_SUSPECT:
        return (f"durações muito diferentes ({dur_a:.0f}s vs {dur_b:.0f}s, "
                f"razão {ratio:.2f}) — possíveis episódios fundidos/divididos; "
                f"confira o mapeamento de arquivos")
    return None


def _residual_of(run, D: np.ndarray, lo: np.ndarray) -> float:
    """Hamming médio ao longo da diagonal do run (só para matches)."""
    _t, i0, i1, j0, _j1 = run
    vals = []
    for step in range(i1 - i0):
        i = i0 + step
        k = (j0 + step) - lo[i]
        if 0 <= k < D.shape[1] and D[i, k] != 255:
            vals.append(float(D[i, k]))
    return float(np.mean(vals)) if vals else 64.0


def classify_runs(runs: list[tuple], D: np.ndarray, lo: np.ndarray,
                  fps: float = FPS) -> list[Segment]:
    """Runs do DP -> segmentos classificados + pós-processamento."""
    segs: list[Segment] = []
    for run in runs:
        t, i0, i1, j0, j1 = run
        if t == dp_mod.GAP_A:
            segs.append(Segment("gap_dub", i0 / fps, i1 / fps,
                                j0 / fps, j0 / fps))
            continue
        if t == dp_mod.GAP_B:
            segs.append(Segment("gap_orig", i0 / fps, i0 / fps,
                                j0 / fps, j1 / fps))
            continue
        n = max(1, i1 - i0)
        residual = _residual_of(run, D, lo)
        slope = (j1 - j0) / n
        if residual > RESIDUAL_REPLACED:
            kind = "replaced"
        elif abs(slope - PAL_SLOPE) <= SLOPE_TOL:
            kind = "pal"
        elif abs(slope - 1.0) > SLOPE_TOL:
            kind = "drift"
        else:
            kind = "match"
        seg = Segment(kind, i0 / fps, i1 / fps, j0 / fps, j1 / fps,
                      slope=slope, residual=residual,
                      confidence=max(0.0, 1.0 - residual / 32.0),
                      offset=(j0 - i0) / fps)
        if kind == "replaced":
            seg.note = "conteúdo divergente com a mesma duração — revisar"
        segs.append(seg)
    return _postprocess(segs, fps, D, lo)


def _postprocess(segs: list[Segment], fps: float,
                 D: np.ndarray | None = None,
                 lo: np.ndarray | None = None) -> list[Segment]:
    """Funde fragmentação espúria e descarta ruído (ordem importa):
    1. descarta gaps < MIN_GAP_S (ruído de hash, não edição);
    2. funde vizinhos do mesmo tipo separados por < MERGE_ADJACENT_S;
    3. funde matches consecutivos cujo offset difere < MERGE_OFFSET_FRAMES."""
    out: list[Segment] = []
    for s in segs:
        dur = max(s.a_end - s.a_start, (s.b_end or 0) - (s.b_start or 0))
        # segmentos abaixo do piso são ruído, de QUALQUER tipo: gap de 1 frame
        # é ruído de hash, e um "match" de 1 frame no meio de uma região
        # divergente é colisão de hash — se ele ficasse, separaria o par de
        # gaps que o _fuse_replaced_pairs precisa ver adjacente
        if dur < MIN_GAP_S:
            continue
        out.append(s)

    def can_merge(a: Segment, b: Segment) -> bool:
        if a.kind != b.kind:
            return False
        if b.a_start - a.a_end > MERGE_ADJACENT_S:
            return False
        if a.kind in ("match", "pal", "drift"):
            if a.offset is None or b.offset is None:
                return False
            return abs(a.offset - b.offset) <= MERGE_OFFSET_FRAMES / fps
        return True

    merged: list[Segment] = []
    for s in out:
        if merged and can_merge(merged[-1], s):
            prev = merged[-1]
            # média ponderada do resíduo pelas durações dos dois trechos
            da = max(1e-9, prev.a_end - prev.a_start)
            db = max(1e-9, s.a_end - s.a_start)
            prev.residual = (prev.residual * da + s.residual * db) / (da + db)
            prev.a_end = s.a_end
            if s.b_end is not None:
                prev.b_end = s.b_end
            prev.confidence = max(0.0, 1.0 - prev.residual / 32.0)
            continue
        merged.append(s)
    return _fuse_replaced_pairs(merged, fps, D, lo)


# par de gaps com durações parecidas dentro desta razão = substituição
_REPLACED_RATIO = 1.4


def _diagonal_residual(a0: float, b0: float, n_frames: int, fps: float,
                       D: np.ndarray | None, lo: np.ndarray | None) -> float:
    """Hamming médio ao longo da diagonal (a0,b0)->(a0+n,b0+n), em frames.
    64 (máximo) quando não há matriz — a UI mostra o número REAL de quão
    diferente o miolo é, em vez de um 0% de confiança por construção."""
    if D is None or lo is None or n_frames <= 0:
        return 64.0
    i0, j0 = int(round(a0 * fps)), int(round(b0 * fps))
    vals = []
    for k in range(n_frames):
        i, j = i0 + k, j0 + k
        if 0 <= i < D.shape[0]:
            kk = j - int(lo[i])
            if 0 <= kk < D.shape[1] and D[i, kk] != 255:
                vals.append(float(D[i, kk]))
    return float(np.mean(vals)) if vals else 64.0


def _fuse_replaced_pairs(segs: list[Segment], fps: float = FPS,
                         D: np.ndarray | None = None,
                         lo: np.ndarray | None = None) -> list[Segment]:
    """gap_dub + gap_orig ADJACENTES com durações parecidas = cena SUBSTITUÍDA.

    Com gap afim barato, o DP não força uma diagonal de resíduo alto sobre
    conteúdo divergente — ele contorna abrindo um gap de cada lado. Esse par
    (material só no dublado + material só no original, do mesmo tamanho, no
    mesmo ponto) é exatamente "mesma duração, conteúdo diferente": funde num
    segmento `replaced`, que SEMPRE exige revisão humana."""
    out: list[Segment] = []
    i = 0
    while i < len(segs):
        s = segs[i]
        nxt = segs[i + 1] if i + 1 < len(segs) else None
        pair = {"gap_dub", "gap_orig"}
        if nxt and {s.kind, nxt.kind} == pair:
            da = s.a_end - s.a_start if s.kind == "gap_dub" else nxt.a_end - nxt.a_start
            db = (s.b_end or 0) - (s.b_start or 0) if s.kind == "gap_orig" \
                else (nxt.b_end or 0) - (nxt.b_start or 0)
            if da > 0 and db > 0 and max(da, db) / min(da, db) <= _REPLACED_RATIO:
                gd = s if s.kind == "gap_dub" else nxt
                go = s if s.kind == "gap_orig" else nxt
                # resíduo MEDIDO no miolo (diagonal do trecho): a confiança
                # passa a refletir o quão diferente o conteúdo é de fato — a
                # decisão continua humana, mas o número deixa de ser 0 fixo
                n = int(round(min(da, db) * fps))
                # a diagonal "natural" do par pode estar alguns frames torta
                # (o DP às vezes deixa um match de 1 frame entre os gaps, que o
                # piso de ruído descarta) — mede numa vizinhança de offsets e
                # fica com o melhor; é ESSE offset que vale se virar match
                b_nat = go.b_start or 0.0
                b_end = go.b_end or 0.0
                span = min(8, int(round(abs(da - db) * fps)) + 2)
                res, a0, b0, n = 64.0, gd.a_start, b_nat, 0
                for d in range(-span, span + 1):
                    # começa DENTRO dos dois gaps (avança um dos lados) e mede
                    # até onde os dois ainda têm frames
                    ca = gd.a_start + max(0, -d) / fps
                    cb = b_nat + max(0, d) / fps
                    nc = int(round(min(gd.a_end - ca, b_end - cb) * fps))
                    if nc < 2:
                        continue
                    r = _diagonal_residual(ca, cb, nc, fps, D, lo)
                    if r < res:
                        res, a0, b0, n = r, ca, cb, nc
                if D is not None and res <= RESIDUAL_REPLACED:
                    # FALSO POSITIVO: o miolo é o MESMO conteúdo (plano parado
                    # em duas encodes distintas, com resíduo um pouco acima do
                    # limiar de match) — o DP achou mais barato contornar com
                    # dois gaps do que atravessar pagando a penalidade frame a
                    # frame. Vira MATCH com o offset do trecho; a diferença de
                    # duração (a fatia que sobra de um dos lados) fica como o
                    # gap pequeno que ela realmente é.
                    dur = n / fps
                    # sobras (< span frames) de cada lado viram gaps pequenos —
                    # os menores que o piso de ruído somem no próximo passo
                    if a0 > gd.a_start + 1e-6:
                        out.append(Segment("gap_dub", gd.a_start, a0, b0, b0))
                    if b0 > b_nat + 1e-6:
                        out.append(Segment("gap_orig", a0, a0, b_nat, b0))
                    out.append(Segment(
                        "match", a0, a0 + dur, b0, b0 + dur,
                        slope=1.0, residual=res,
                        confidence=max(0.0, 1.0 - res / 32.0),
                        offset=b0 - a0,
                        note="par de gaps refundido: miolo igual"))
                    if gd.a_end > a0 + dur + 1e-6:
                        out.append(Segment("gap_dub", a0 + dur, gd.a_end,
                                           b0 + dur, b0 + dur))
                    if b_end > b0 + dur + 1e-6:
                        out.append(Segment("gap_orig", a0 + dur, a0 + dur,
                                           b0 + dur, b_end))
                    i += 2
                    continue
                out.append(Segment(
                    "replaced", gd.a_start, gd.a_end, go.b_start, go.b_end,
                    residual=res, confidence=max(0.0, 1.0 - res / 32.0),
                    note="conteúdo divergente com duração parecida "
                         "(cena substituída?) — revisar"))
                i += 2
                continue
        out.append(s)
        i += 1
    # sobras de 1-2 frames criadas pela refusão são ruído, como qualquer gap
    # abaixo do piso — some aqui (o piso já rodou antes desta etapa)
    return [s for s in out
            if not (s.kind in ("gap_dub", "gap_orig")
                    and max(s.a_end - s.a_start,
                            (s.b_end or 0) - (s.b_start or 0)) < MIN_GAP_S)]


def confidence_profile(segs: list[Segment], duration_a: float,
                       fps: float = FPS) -> list[float]:
    """Confiança por SEGUNDO do episódio (timeline do dublado) — alimenta a
    visualização de revisão e o relatório em lote ("quanto trabalho manual
    este episódio vai dar")."""
    n = max(1, int(round(duration_a)))
    prof = [0.0] * n
    for s in segs:
        conf = s.confidence if s.kind in ("match", "pal", "drift") else (
            0.0 if s.kind == "replaced" else 0.5)  # gap: decisão, não incerteza
        for sec in range(int(s.a_start), min(n, int(np.ceil(s.a_end)))):
            prof[sec] = max(prof[sec], conf)
    return prof


def needs_review(segs: list[Segment]) -> list[Segment]:
    """Segmentos que EXIGEM decisão humana (a regra do produto: substituição
    nunca é resolvida sozinha)."""
    return [s for s in segs if s.kind == "replaced"]
