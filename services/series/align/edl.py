"""Estágio 5: a EDL (edit decision list) — o produto do alinhador.

JSON versionado, persistido dentro do job (job["episodes"][k]["edl"]). O
campo `offset` é redundante com a_start/b_start de propósito: o merge não
recalcula nada e a inspeção humana lê direto. `review.required` TRAVA o
avanço do job — substituição nunca passa sem decisão humana.
"""
from dataclasses import asdict

from services.series.align.classify import Segment, needs_review

VERSION = 1


def build(segs: list[Segment], episode: str,
          src_dub: str, dur_dub: float,
          src_orig: str, dur_orig: float,
          profile: list[float] | None = None) -> dict:
    flagged = needs_review(segs)
    return {
        "version": VERSION,
        "episode": episode,
        "source_dub": {"path": src_dub, "duration": round(dur_dub, 3)},
        "source_orig": {"path": src_orig, "duration": round(dur_orig, 3)},
        "segments": [_seg_dict(s) for s in segs],
        "confidence_profile": ([round(c, 3) for c in profile]
                               if profile is not None else None),
        "review": {
            "required": bool(flagged),
            "flagged": [{"a_start": s.a_start, "a_end": s.a_end,
                         "reason": s.kind} for s in flagged],
        },
    }


def _seg_dict(s: Segment) -> dict:
    d = asdict(s)
    extra = d.pop("extra", None) or {}
    # decisão de revisão (fill_original/silence/use_dub/accept) persiste com a
    # EDL — o render lê daqui depois que o gate resolve
    if extra.get("action"):
        d["action"] = extra["action"]
    if extra.get("refine"):
        d["refine"] = extra["refine"]
    # o resto do extra (junção exata da cena cortada: junction_a/b0/b1...)
    # persiste inteiro — o render lê a EDL guardada, não os Segments vivos
    rest = {k: v for k, v in extra.items() if k not in ("action", "refine")}
    if rest:
        d["extra"] = rest
    for key in ("a_start", "a_end", "b_start", "b_end", "offset"):
        if d.get(key) is not None:
            d[key] = round(d[key], 3)
    for key in ("slope", "residual", "confidence"):
        if d.get(key) is not None:
            d[key] = round(d[key], 4)
    return d


def segments(edl: dict) -> list[Segment]:
    """EDL persistida -> Segments (para o renderer/refino)."""
    out = []
    for d in edl.get("segments", []):
        s = Segment(
            kind=d["kind"], a_start=d["a_start"], a_end=d["a_end"],
            b_start=d.get("b_start"), b_end=d.get("b_end"),
            slope=d.get("slope"), residual=d.get("residual", 0.0),
            confidence=d.get("confidence", 1.0), offset=d.get("offset"),
            note=d.get("note", ""))
        if d.get("action"):
            s.extra["action"] = d["action"]
        if d.get("refine"):
            s.extra["refine"] = d["refine"]
        if isinstance(d.get("extra"), dict):
            s.extra.update(d["extra"])
        out.append(s)
    return out


def stats(edl: dict) -> dict:
    """Números do relatório em lote: % do episódio coberto por match, resíduo
    médio dos matches e contagens por tipo."""
    segs = edl.get("segments", [])
    dur = max(1e-9, edl["source_dub"]["duration"])
    match_time = sum(s["a_end"] - s["a_start"] for s in segs
                     if s["kind"] in ("match", "pal", "drift"))
    matches = [s for s in segs if s["kind"] in ("match", "pal", "drift")]
    kinds: dict[str, int] = {}
    for s in segs:
        kinds[s["kind"]] = kinds.get(s["kind"], 0) + 1
    return {
        "match_pct": round(100.0 * match_time / dur, 1),
        "mean_residual": round(sum(s["residual"] or 0 for s in matches)
                               / len(matches), 2) if matches else None,
        "kinds": kinds,
        "review_required": edl["review"]["required"],
    }
