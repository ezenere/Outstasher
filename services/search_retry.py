"""Busca programada e contínua: a política global e o que cada job sobrepõe.

Um job pode nascer PROGRAMADO (status "scheduled"): filme/episódio com data e
hora marcadas, ou episódio futuro esperando o dia do lançamento. E um job cuja
busca não deu em nada que sirva (nada encontrado, nada na qualidade mínima,
nenhum par do mesmo corte) volta a "scheduled" para tentar de novo depois de
um intervalo — a busca contínua — até achar ou ser excluído. Enquanto espera,
a lista do que já apareceu continua lá para a escolha à mão / magnet próprio.

Política global em settings (JSON), editável na tela de Configurações:
- `enabled`: busca contínua ligada (o padrão de todo job);
- `minutes`: intervalo entre as tentativas;
- `release_time`: hora do dia do lançamento em que um episódio futuro começa
  a ser procurado ("HH:MM", horário do servidor — o mesmo da UI).

Override por job: `retry_mode` "on"/"off" vence o `enabled` global; ausente
herda. As datas do job são ISO local sem fuso (como `created_at`): o container
roda no fuso do usuário.
"""
import json
import re
from datetime import date, datetime, time, timedelta

from services import store

KEY = "search_retry"
MODES = ("on", "off")
MAX_MINUTES = 30 * 24 * 60
_FIELDS = ("enabled", "minutes", "release_time")
_HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def defaults() -> dict:
    return {"enabled": True, "minutes": 6 * 60, "release_time": "06:00"}


def validate_release_time(v) -> str:
    if not isinstance(v, str) or not _HHMM.match(v):
        raise ValueError("hora do lançamento: use HH:MM (00:00 a 23:59)")
    return v


def validate(cfg: dict) -> dict:
    if not isinstance(cfg, dict):
        raise ValueError("configuração da busca contínua: objeto esperado")
    base = defaults()
    out = {"enabled": cfg.get("enabled", base["enabled"])}
    if not isinstance(out["enabled"], bool):
        raise ValueError("enabled: true ou false")
    m = cfg.get("minutes", base["minutes"])
    if isinstance(m, bool) or not isinstance(m, (int, float)) or m != int(m):
        raise ValueError("intervalo: informe um número inteiro de minutos")
    if not 1 <= int(m) <= MAX_MINUTES:
        raise ValueError(f"intervalo: entre 1 minuto e {MAX_MINUTES // 1440} dias")
    out["minutes"] = int(m)
    out["release_time"] = validate_release_time(
        cfg.get("release_time", base["release_time"]))
    return out


def get() -> dict:
    raw = store.get_setting(KEY)
    if not raw:
        return defaults()
    try:
        data = json.loads(raw)
        return validate({k: data[k] for k in _FIELDS if k in data})
    except (ValueError, TypeError, KeyError):
        return defaults()


def set(cfg: dict) -> dict:  # noqa: A001 — espelha get()
    clean = validate(cfg)
    store.set_setting(KEY, json.dumps(clean))
    return clean


def validate_mode(mode) -> str | None:
    if mode in (None, "", "inherit"):
        return None
    if mode not in MODES:
        raise ValueError(f"busca contínua: modo inválido {mode!r} (inherit, on ou off)")
    return mode


def enabled_for(job: dict) -> bool:
    """A busca contínua vale para este job?"""
    mode = job.get("retry_mode")
    if mode == "off":
        return False
    return mode == "on" or get()["enabled"]


# -------------------- datas --------------------

def now() -> datetime:
    return datetime.now().replace(microsecond=0)


def fmt(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def parse_when(v) -> datetime:
    """Data/hora vinda da UI ("2026-11-10T20:00", com ou sem fuso) -> local
    sem fuso, como o resto das datas do job."""
    if not isinstance(v, str) or not v.strip():
        raise ValueError("data/hora da programação: informe AAAA-MM-DDTHH:MM")
    try:
        dt = datetime.fromisoformat(v.strip().replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"data/hora da programação inválida: {v!r}")
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt.replace(microsecond=0)


def release_at(air_date: str | None, release_time: str | None = None) -> datetime | None:
    """Quando procurar um episódio que estreia em `air_date`: nesse dia, na
    hora pedida (ou na da configuração). None se o TMDB não tem a data."""
    if not air_date:
        return None
    try:
        d = date.fromisoformat(air_date)
    except ValueError:
        return None
    hh, mm = (release_time or get()["release_time"]).split(":")
    return datetime.combine(d, time(int(hh), int(mm)))


def next_retry() -> datetime:
    return now() + timedelta(minutes=get()["minutes"])


def human(dt_iso: str | None) -> str:
    """"10/11 às 20:00" para os textos do job."""
    if not dt_iso:
        return "—"
    try:
        dt = datetime.fromisoformat(dt_iso)
    except ValueError:
        return dt_iso
    return dt.strftime("%d/%m às %H:%M")
