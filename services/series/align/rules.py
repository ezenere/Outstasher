"""Regras de revisão reaplicáveis ("aplicar a todos os episódios").

Numa temporada, a mesma decisão estrutural (recap no início → descartar;
créditos diferentes no fim → preencher com áudio original) vale para 20+
episódios — refazê-la 20 vezes é o que torna a revisão insuportável. Por isso
as decisões são REGRAS sobre a forma do segmento (tipo, posição, faixa de
duração), não timestamps de um episódio específico.

Regra: {"when": {"kind", "position": "start|end|middle|any",
                 "min_len": s, "max_len": s},
        "action": "fill_original" | "silence" | "use_dub" | "accept"}

Ações por tipo de segmento:
- gap_orig  (vídeo sem dublagem):   fill_original (default) | silence |
                                    cut_video (a cena SAI do vídeo)
- replaced  (conteúdo divergente):  fill_original | use_dub | silence |
                                    cut_video (a cena sai; a dublagem daquele
                                    trecho, que dublava OUTRA cena, é descartada)
- gap_dub   (dublado sem vídeo):    sempre descartado (a timeline é a do
                                    original); "accept" só o marca revisado

cut_video corta em keyframes por stream copy (mkvmerge): raspas de até um
GOP (~2 s) ficam nas bordas com áudio original — nada é recodificado.
"""
from services.series.align.classify import Segment

VALID_ACTIONS = ("fill_original", "silence", "use_dub", "cut_video", "accept")
# fração da duração do episódio que conta como "início"/"fim" para position
EDGE_FRACTION = 0.15


def _position(seg: Segment, duration_a: float) -> str:
    if seg.a_start <= duration_a * EDGE_FRACTION:
        return "start"
    if seg.a_end >= duration_a * (1 - EDGE_FRACTION):
        return "end"
    return "middle"


def _matches(rule: dict, seg: Segment, duration_a: float) -> bool:
    when = rule.get("when") or {}
    if when.get("kind") and when["kind"] != seg.kind:
        return False
    pos = when.get("position", "any")
    if pos != "any" and _position(seg, duration_a) != pos:
        return False
    dur = max(seg.a_end - seg.a_start, (seg.b_end or 0) - (seg.b_start or 0))
    if when.get("min_len") is not None and dur < when["min_len"]:
        return False
    if when.get("max_len") is not None and dur > when["max_len"]:
        return False
    return True


def validate_rules(rules: list[dict]) -> list[dict]:
    out = []
    for r in rules or []:
        if r.get("action") not in VALID_ACTIONS:
            raise ValueError(f"ação inválida na regra: {r.get('action')!r}")
        if not isinstance(r.get("when"), dict):
            raise ValueError("regra sem 'when'")
        out.append(r)
    return out


def apply_rules(segs: list[Segment], rules: list[dict],
                duration_a: float) -> tuple[list[Segment], bool]:
    """Aplica as regras aos segmentos (seta seg.extra["action"]).

    Retorna (segs, ainda_precisa_de_revisao): sobra revisão quando algum
    `replaced` ficou SEM ação — substituição nunca passa sem decisão."""
    for seg in segs:
        if seg.extra.get("action"):
            continue  # decisão explícita do usuário vence regra
        for rule in rules or []:
            if _matches(rule, seg, duration_a):
                seg.extra["action"] = rule["action"]
                seg.extra["action_from"] = "regra"
                break
    needs = any(s.kind == "replaced" and not s.extra.get("action")
                for s in segs)
    return segs, needs
