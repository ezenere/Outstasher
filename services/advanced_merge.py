"""Configuração do MERGE AVANÇADO (alinhamento por conteúdo): o que fazer com
trecho sem dublagem e como re-encodar quando cenas são cortadas do vídeo.

Guardada como JSON em settings. A política vira REGRAS PADRÃO aplicadas
depois das regras de revisão do usuário (decisão explícita sempre vence).
"""
import json

from services import store

KEY = "advanced_merge"
UNDUBBED = ("cut", "silence", "fill")          # trecho sem dublagem
DEFAULTS = {
    "undubbed": "cut",         # sem dublagem: a cena SAI do vídeo…
    "cut_min_s": 1.0,          # …a partir disto; abaixo, fica MUDA
    # re-encode nos cortes: as MESMAS opções de conversão do app (codec,
    # encoder, preset, CRF/bitrate...) ou None = corte em keyframe. O padrão
    # é decidido pela máquina em default_reencode(): AV1 na GPU se houver.
    "reencode": None,
}


def default_reencode() -> dict:
    """Padrão do re-encode para ESTA máquina: AV1 CRF 20 na GPU Intel se ela
    responde, senão SVT-AV1 em CPU (lento, mas exato)."""
    from services import transcode
    hw = "qsv" if transcode.hw_encoder_works("av1_qsv") else "none"
    return transcode.validate({"video_codec": "av1", "hw_accel": hw,
                               "quality_mode": "crf", "crf": 20,
                               "preset": "default"}).to_dict()


def get() -> dict:
    raw = store.get_setting(KEY)
    if not raw:
        cfg = dict(DEFAULTS)
        cfg["reencode"] = default_reencode()
        return cfg
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        data = {}
    cfg = dict(DEFAULTS)
    if isinstance(data, dict):
        cfg.update({k: v for k, v in data.items() if k in DEFAULTS})
    return validate(cfg)


def validate(cfg: dict) -> dict:
    from services import transcode
    out = dict(DEFAULTS)
    und = str(cfg.get("undubbed", out["undubbed"]))
    if und not in UNDUBBED:
        raise ValueError(f"política inválida para trecho sem dublagem: {und!r}")
    out["undubbed"] = und
    try:
        out["cut_min_s"] = max(0.0, float(cfg.get("cut_min_s", out["cut_min_s"])))
    except (TypeError, ValueError):
        raise ValueError("cut_min_s precisa ser um número de segundos")
    re_ = cfg.get("reencode")
    if re_ in (None, "", "none"):
        out["reencode"] = None
    elif isinstance(re_, dict):
        opts = transcode.validate(re_)          # levanta ValueError se inválido
        if opts.video_codec == "keep":
            raise ValueError("re-encode nos cortes precisa de um codec de vídeo "
                             "(ou desligue o re-encode)")
        out["reencode"] = opts.to_dict()
    else:
        raise ValueError("re-encode: use as opções de conversão ou null")
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
    if has_cuts and cfg.get("reencode"):
        kw["video_reencode"] = {"convert": cfg["reencode"]}
    return kw


def for_job(job: dict) -> dict:
    """Política efetiva de um job: a global, com o override do job por cima
    (escolhido no modal de download ou na hora de pedir o merge avançado).
    job["advanced_merge"] = None/ausente → só a global."""
    base = get()
    over = job.get("advanced_merge")
    if not isinstance(over, dict) or not over:
        return base
    merged = dict(base)
    merged.update({k: v for k, v in over.items() if k in DEFAULTS})
    return validate(merged)


def validate_override(over) -> dict | None:
    """Override parcial vindo da API: None/{} = usar a global; senão valida
    as chaves informadas contra a global (o resto herda)."""
    if not over:
        return None
    if not isinstance(over, dict):
        raise ValueError("advanced_merge: objeto ou null")
    merged = dict(get())
    merged.update({k: v for k, v in over.items() if k in DEFAULTS})
    ok = validate(merged)
    return {k: ok[k] for k in over if k in DEFAULTS}
