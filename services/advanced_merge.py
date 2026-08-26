"""Configuração do MERGE AVANÇADO (alinhamento por conteúdo): o que fazer com
trecho sem dublagem e como re-encodar quando cenas são cortadas do vídeo.

Guardada como JSON em settings. A política vira REGRAS PADRÃO aplicadas
depois das regras de revisão do usuário (decisão explícita sempre vence).
"""
import json

from services import store

KEY = "advanced_merge"
UNDUBBED = ("cut", "silence", "fill")          # trecho sem dublagem
REENCODE = ("auto", "av1_qsv", "libsvtav1", "none")
DEFAULTS = {
    "undubbed": "cut",         # sem dublagem: a cena SAI do vídeo…
    "cut_min_s": 1.0,          # …a partir disto; abaixo, fica MUDA
    "reencode": "auto",        # corte frame-exato re-encodando o vídeo
    "quality": 20,             # CRF / ICQ do re-encode
}


def get() -> dict:
    raw = store.get_setting(KEY)
    cfg = dict(DEFAULTS)
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                cfg.update({k: v for k, v in data.items() if k in DEFAULTS})
        except (ValueError, TypeError):
            pass
    return validate(cfg)


def validate(cfg: dict) -> dict:
    out = dict(DEFAULTS)
    und = str(cfg.get("undubbed", out["undubbed"]))
    if und not in UNDUBBED:
        raise ValueError(f"política inválida para trecho sem dublagem: {und!r}")
    out["undubbed"] = und
    try:
        out["cut_min_s"] = max(0.0, float(cfg.get("cut_min_s", out["cut_min_s"])))
    except (TypeError, ValueError):
        raise ValueError("cut_min_s precisa ser um número de segundos")
    re_ = str(cfg.get("reencode", out["reencode"]))
    if re_ not in REENCODE:
        raise ValueError(f"re-encode inválido: {re_!r}")
    out["reencode"] = re_
    try:
        q = int(cfg.get("quality", out["quality"]))
    except (TypeError, ValueError):
        raise ValueError("qualidade precisa ser um inteiro")
    if not 1 <= q <= 63:
        raise ValueError("qualidade fora da faixa (1-63)")
    out["quality"] = q
    return out


def set(cfg: dict) -> dict:  # noqa: A001 — espelha get()
    clean = validate(cfg)
    store.set_setting(KEY, json.dumps(clean))
    return clean


def default_rules(cfg: dict | None = None) -> list[dict]:
    """Regras padrão derivadas da política — vão DEPOIS das regras do usuário
    (apply_rules pula segmento que já tem ação)."""
    cfg = cfg or get()
    if cfg["undubbed"] == "fill":
        return []          # o default do render já é preencher com o original
    if cfg["undubbed"] == "silence":
        return [{"when": {"kind": "gap_orig"}, "action": "silence"}]
    # cut: cena sem dublagem sai do vídeo a partir de cut_min_s; abaixo, muda
    rules = [{"when": {"kind": "gap_orig", "min_len": cfg["cut_min_s"]},
              "action": "cut_video"}]
    if cfg["cut_min_s"] > 0:
        rules.append({"when": {"kind": "gap_orig", "max_len": cfg["cut_min_s"]},
                      "action": "silence"})
    return rules


def render_kwargs(cfg: dict | None = None, has_cuts: bool = False) -> dict:
    """Argumentos extras do render.render() para esta política."""
    cfg = cfg or get()
    kw = {"fill_with_original": cfg["undubbed"] == "fill"}
    if has_cuts and cfg["reencode"] != "none":
        kw["video_reencode"] = {"codec": "av1", "encoder": cfg["reencode"],
                                "crf": cfg["quality"]}
    return kw


def encoders_available() -> dict:
    """Para a UI: o que esta máquina consegue usar."""
    from services import transcode
    return {
        "av1_qsv": transcode.hw_encoder_works("av1_qsv"),
        "libsvtav1": "libsvtav1" in transcode.available_encoders(),
    }
