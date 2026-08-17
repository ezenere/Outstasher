"""Orquestra os estágios 0-3 para UM par de arquivos (dublado x original).

Produz a EDL; quem decide o que fazer com ela (render, gate de revisão,
relatório em lote) é o chamador. Erros de MAPEAMENTO (durações incompatíveis,
nenhum match) viram exceções específicas — o pipeline os transforma em gate,
nunca em decisão automática.
"""
from pathlib import Path

from services import merger
from services.series.align import classify, dp, edl, fingerprint


class AlignError(RuntimeError):
    """Falha técnica do alinhador (ffmpeg, arquivo ilegível...)."""


class AlignConflict(RuntimeError):
    """O par NÃO é alinhável como está (episódios fundidos/divididos, ordem
    trocada). Não é bug: é decisão do usuário — vira gate no pipeline."""


def _duration(path: str) -> float:
    probe = merger.ffprobe_json(path)
    try:
        return float(probe["format"]["duration"])
    except (KeyError, TypeError, ValueError):
        raise AlignError(f"sem duração legível em {path}")


# razão de duração que caracteriza um arquivo FUNDIDO (dois episódios num só)
MERGED_RATIO = (1.7, 2.3)
# fração mínima do arquivo curto casada para aceitar uma metade como o par
MIN_MATCH_FRACTION = 0.10
# sobreposição entre as "metades" candidatas — o episódio não começa
# exatamente no meio (cold open, créditos, recap)
HALF_OVERLAP = 0.10


def align_pair(dub_path: str, orig_path: str, episode: str = "?",
               dump_png: str | None = None,
               band: int = fingerprint.BAND) -> dict:
    """Estágios 0-3 completos: EDL do par (sem refino de áudio, sem render).

    dump_png: caminho para gravar a matriz de distância (debug visual).

    Arquivo FUNDIDO (razão de duração ~2 — dois episódios num só, comum em
    releases de estreia dupla): em vez de falhar, o lado curto é alinhado
    contra cada METADE do longo e fica a que casa melhor. A EDL registra a
    janela usada (`a_window`/`b_window`, em segundos ABSOLUTOS do arquivo
    longo); quando o fundido é o ORIGINAL, o render corta o vídeo nessa
    janela. Razão fora dessa faixa continua sendo conflito (é problema de
    mapeamento de arquivos, não de alinhamento).
    """
    dur_a = _duration(dub_path)
    dur_b = _duration(orig_path)
    conflict = classify.check_duration_ratio(dur_a, dur_b)
    ratio = max(dur_a, dur_b) / max(1e-9, min(dur_a, dur_b))
    merged = bool(conflict) and MERGED_RATIO[0] <= ratio <= MERGED_RATIO[1]
    if conflict and not merged:
        raise AlignConflict(conflict)

    # estágio 0: normalização geométrica de CADA lado antes do hash
    crop_a = fingerprint.crop_params(dub_path, dur_a)
    crop_b = fingerprint.crop_params(orig_path, dur_b)
    # estágio 1: fingerprint (dos arquivos inteiros)
    ha = fingerprint.dhash_stream(dub_path, crop_a)
    hb = fingerprint.dhash_stream(orig_path, crop_b)

    if not merged:
        D, lo = fingerprint.hamming_band(ha, hb, band=band)
        if dump_png:
            Path(dump_png).parent.mkdir(parents=True, exist_ok=True)
            fingerprint.dump_matrix_png(D, dump_png)
        runs = dp.align_band(D, lo, len(hb))
        _check_match_fraction(runs, min(len(ha), len(hb)))
        segs = classify.classify_runs(runs, D, lo)
        profile = classify.confidence_profile(segs, dur_a)
        return edl.build(segs, episode, dub_path, dur_a, orig_path, dur_b,
                         profile=profile)

    # ---- fundido: alinha o curto contra cada metade do longo ----
    long_is_b = dur_b > dur_a
    long_h = hb if long_is_b else ha
    fps = fingerprint.FPS
    n = len(long_h)
    halves = [(0, int(n * (0.5 + HALF_OVERLAP))),
              (int(n * (0.5 - HALF_OVERLAP)), n)]
    best = None
    for h0, h1 in halves:
        piece = long_h[h0:h1]
        if long_is_b:
            D, lo = fingerprint.hamming_band(ha, piece, band=band)
            runs = dp.align_band(D, lo, len(piece))
        else:
            D, lo = fingerprint.hamming_band(piece, hb, band=band)
            runs = dp.align_band(D, lo, len(hb))
        mf = sum(r[2] - r[1] for r in runs if r[0] == dp.MATCH)
        if best is None or mf > best[0]:
            best = (mf, h0, h1, D, lo, runs)
    mf, h0, h1, D, lo, runs = best
    short_len = min(len(ha), len(hb))
    if mf < MIN_MATCH_FRACTION * short_len:
        raise AlignConflict(
            f"durações sugerem arquivo fundido (razão {ratio:.2f}) mas o "
            f"episódio curto não casa com nenhuma das metades do longo — "
            f"confira o mapeamento de arquivos")
    if dump_png:
        Path(dump_png).parent.mkdir(parents=True, exist_ok=True)
        fingerprint.dump_matrix_png(D, dump_png)

    # os runs estão em frames da METADE: desloca o lado longo para o absoluto
    shift = h0
    if long_is_b:
        runs = [(t, i0, i1, j0 + shift, j1 + shift) for t, i0, i1, j0, j1 in runs]
        # e a matriz também precisa "enxergar" o deslocamento no resíduo:
        # lo indexa colunas da metade -> soma o shift
        lo = lo + shift
    else:
        runs = [(t, i0 + shift, i1 + shift, j0, j1) for t, i0, i1, j0, j1 in runs]
        # linhas deslocadas: acolchoa D/lo com linhas fora da banda para as
        # linhas do lado curto do começo (índices absolutos)
        import numpy as np
        pad_D = np.full((shift, D.shape[1]), 255, dtype=D.dtype)
        D = np.vstack([pad_D, D])
        lo = np.concatenate([np.zeros(shift, dtype=lo.dtype), lo])
    segs = classify.classify_runs(runs, D, lo)
    # janela do lado longo realmente usada: do primeiro ao último trecho
    # CASADO (match/replaced). Gap "só no longo" nas pontas é o rabo/começo
    # do OUTRO episódio, não deste — fica de fora (com 2 s de margem)
    matched = [s for s in segs if s.kind in ("match", "pal", "drift", "replaced")
               and s.b_start is not None and s.b_end is not None]
    if long_is_b:
        window = ((max(0.0, min(s.b_start for s in matched) - 2.0),
                   min(dur_b, max(s.b_end for s in matched) + 2.0))
                  if matched else (h0 / fps, h1 / fps))
    else:
        window = ((max(0.0, min(s.a_start for s in matched) - 2.0),
                   min(dur_a, max(s.a_end for s in matched) + 2.0))
                  if matched else (h0 / fps, h1 / fps))
    profile = classify.confidence_profile(segs, dur_a)
    out = edl.build(segs, episode, dub_path, dur_a, orig_path, dur_b,
                    profile=profile)
    out["merged_side"] = "orig" if long_is_b else "dub"
    out["b_window" if long_is_b else "a_window"] = [round(window[0], 3),
                                                       round(window[1], 3)]
    out["note"] = (f"arquivo {'original' if long_is_b else 'dublado'} fundido "
                   f"(razão {ratio:.2f}): episódio localizado na "
                   f"{'1ª' if h0 == 0 else '2ª'} metade "
                   f"({window[0]:.0f}s–{window[1]:.0f}s)")
    return out


def _check_match_fraction(runs, short_len: int):
    # conteúdo sem relação ainda produz matches ESPARSOS (frames escuros/
    # parecidos por acaso) — o critério é a FRAÇÃO coberta por match, não a
    # existência de algum
    match_frames = sum(r[2] - r[1] for r in runs if r[0] == dp.MATCH)
    if match_frames < MIN_MATCH_FRACTION * short_len:
        raise AlignConflict(
            "quase nada dos dois arquivos casa — sintoma clássico de ordem "
            "de episódios trocada (TV vs DVD/absoluta) ou de arquivos de "
            "episódios diferentes; confira o mapeamento antes de insistir")
