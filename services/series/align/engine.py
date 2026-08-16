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


def align_pair(dub_path: str, orig_path: str, episode: str = "?",
               dump_png: str | None = None,
               band: int = fingerprint.BAND) -> dict:
    """Estágios 0-3 completos: EDL do par (sem refino de áudio, sem render).

    dump_png: caminho para gravar a matriz de distância (debug visual).
    """
    dur_a = _duration(dub_path)
    dur_b = _duration(orig_path)
    conflict = classify.check_duration_ratio(dur_a, dur_b)
    if conflict:
        raise AlignConflict(conflict)

    # estágio 0: normalização geométrica de CADA lado antes do hash
    crop_a = fingerprint.crop_params(dub_path, dur_a)
    crop_b = fingerprint.crop_params(orig_path, dur_b)
    # estágio 1: fingerprint + matriz de distância em banda
    ha = fingerprint.dhash_stream(dub_path, crop_a)
    hb = fingerprint.dhash_stream(orig_path, crop_b)
    D, lo = fingerprint.hamming_band(ha, hb, band=band)
    if dump_png:
        Path(dump_png).parent.mkdir(parents=True, exist_ok=True)
        fingerprint.dump_matrix_png(D, dump_png)

    # estágio 2: DP com gap afim
    runs = dp.align_band(D, lo, len(hb))
    # conteúdo sem relação ainda produz matches ESPARSOS (frames escuros/
    # parecidos por acaso) — o critério é a FRAÇÃO coberta por match, não a
    # existência de algum
    match_frames = sum(r[2] - r[1] for r in runs if r[0] == dp.MATCH)
    if match_frames < 0.10 * min(len(ha), len(hb)):
        raise AlignConflict(
            "quase nada dos dois arquivos casa — sintoma clássico de ordem "
            "de episódios trocada (TV vs DVD/absoluta) ou de arquivos de "
            "episódios diferentes; confira o mapeamento antes de insistir")

    # estágio 3: classificação + pós-processamento + perfil de confiança
    segs = classify.classify_runs(runs, D, lo)
    profile = classify.confidence_profile(segs, dur_a)
    return edl.build(segs, episode, dub_path, dur_a, orig_path, dur_b,
                     profile=profile)
