"""Troca automática de torrent travado: a política global e o override por
torrent.

O watchdog (filme: jobs/downloads.py; série: series/pipeline.py) troca o
torrent que fica sem progresso por um candidato reserva. A política global
mora em settings (JSON); enquanto ninguém a salvou na tela de Configurações,
vale a do .env (STALL_TIMEOUT_MINUTES, 0 = desligada) — quem já usava não vê
mudança nenhuma.

A paciência por estado do qBittorrent continua, mas como campo próprio:
- `minutes`: sem progresso baixando (o limite "de verdade");
- `metadata_minutes`: magnet ainda buscando metadados (metaDL) — demora
  mesmo, sem que o torrent esteja morto;
- `stalled_minutes`: sem fonte AGORA (stalledDL) — o estado mais comum de
  torrent parado, e o que mais volta sozinho.
Antes eram pisos fixos (30 e 120 min) sobre o limite. Proporcional ao limite
mudaria o comportamento de quem tem outro valor no .env, e o piso fixo
escondido fazia "5 min" na tela não valer para o caso mais comum (stalledDL
seguiria esperando 2 h): com os três à vista, o padrão é exatamente o de
antes e o usuário vê (e muda) o que vale. Os dois estados nunca ficam abaixo
de `minutes`.

Override por torrent (filme: por papel, chaves stall_mode_<papel> /
stall_minutes_<papel> do job; série: stall_mode / stall_minutes do torrent
t<n>): `mode` "on"/"off" vence o `enabled` global; ausente herda. `minutes`
próprio vale para qualquer estado — é a decisão explícita para aquele
torrent. Lido a cada tick do watchdog: a mudança vale no próximo.
"""
import json

import config
from services import store

KEY = "stall_switch"
MODES = ("on", "off")
MAX_MINUTES = 7 * 24 * 60
_FIELDS = ("enabled", "minutes", "metadata_minutes", "stalled_minutes")


def defaults() -> dict:
    """A política que vale enquanto nada foi salvo: a do .env, com a mesma
    paciência por estado de sempre (piso de 30 min no metaDL e 2 h no
    stalledDL)."""
    env = config.STALL_TIMEOUT_MINUTES
    base = env if env > 0 else 15      # desligada no .env: 15 se ligarem na tela
    return {"enabled": env > 0, "minutes": base,
            "metadata_minutes": max(base, 30), "stalled_minutes": max(base, 120)}


def _minutes(v, what: str) -> int:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != int(v):
        raise ValueError(f"{what}: informe um número inteiro de minutos")
    if not 1 <= int(v) <= MAX_MINUTES:
        raise ValueError(f"{what}: entre 1 e {MAX_MINUTES} minutos")
    return int(v)


def validate(cfg: dict) -> dict:
    if not isinstance(cfg, dict):
        raise ValueError("configuração da troca automática: objeto esperado")
    base = defaults()
    out = {"enabled": cfg.get("enabled", base["enabled"])}
    if not isinstance(out["enabled"], bool):
        raise ValueError("enabled: true ou false")
    for k, what in (("minutes", "limite sem progresso"),
                    ("metadata_minutes", "limite buscando metadados"),
                    ("stalled_minutes", "limite sem fontes")):
        out[k] = _minutes(cfg.get(k, base[k]), what)
    return out


def get() -> dict:
    raw = store.get_setting(KEY)
    if not raw:
        return defaults()
    try:
        data = json.loads(raw)
        return validate({k: data[k] for k in _FIELDS if k in data})
    except (ValueError, TypeError, KeyError):
        return defaults()     # gravação corrompida: a do .env


def set(cfg: dict) -> dict:  # noqa: A001 — espelha get()
    clean = validate(cfg)
    store.set_setting(KEY, json.dumps(clean))
    return clean


def validate_override(mode, minutes) -> tuple[str | None, int | None]:
    """Override por torrent vindo da API: mode None/"inherit" = herda."""
    if mode in (None, "", "inherit"):
        mode = None
    elif mode not in MODES:
        raise ValueError(f"modo inválido: {mode!r} (use inherit, on ou off)")
    if minutes is not None:
        minutes = _minutes(minutes, "limite deste torrent")
    return mode, minutes


def limit_minutes(cfg: dict, state: str, mode: str | None = None,
                  minutes: int | None = None) -> int | None:
    """Minutos sem progresso até a troca automática deste torrent, no estado
    atual do qBittorrent; None = não troca (desligada nele ou na global)."""
    if mode == "off" or (mode != "on" and not cfg["enabled"]):
        return None
    if minutes:
        return minutes
    s = (state or "").lower()
    if "metadl" in s:       # metaDL / forcedMetaDL
        return max(cfg["minutes"], cfg["metadata_minutes"])
    if "stalleddl" in s:    # stalledDL
        return max(cfg["minutes"], cfg["stalled_minutes"])
    return cfg["minutes"]
