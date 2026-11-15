"""Dono e permissões do que o Outstasher grava no disco de mídia.

O container roda como root: sem isto, filme, legenda e as pastas criadas
saem root:root 644/755, e quem mais usa a biblioteca (o usuário pelo SSH ou
SMB, outros serviços do mesmo grupo) não consegue mexer neles. As regras vêm
do ambiente:

  PUID       dono (número; nome só se o usuário existir no container)
  PGID       grupo (idem)
  FILE_MODE  permissão dos arquivos gravados, em octal (ex.: 775)
  DIR_MODE   permissão das pastas CRIADAS pelo app, em octal (ex.: 775)

Variável vazia: aquele aspecto fica como o sistema deixar (o comportamento
antigo). Pasta que já existia não é tocada, só as que o app criou. Falha de
chown/chmod (sem permissão, FS sem suporte a dono) vira aviso no log e nunca
derruba o job: o arquivo entregue vale mais que o dono dele.

Hardlink: o arquivo entregue por hardlink É o do torrent (mesmo inode), então
a regra vale para os dois.
"""
import grp
import logging
import os
import pwd
from pathlib import Path

logger = logging.getLogger(__name__)
_warned: set[str] = set()


def _warn_once(key: str, msg: str):
    if key not in _warned:
        _warned.add(key)
        logger.warning(msg)


def _parse_id(name: str, lookup) -> int | None:
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    if raw.lstrip("-").isdigit():
        return int(raw)
    try:
        return lookup(raw)
    except KeyError:
        _warn_once(f"id:{name}:{raw}",
                   f"{name}={raw!r}: não existe no container; use o número "
                   f"(ex.: id -u / getent group no host). Regra ignorada.")
        return None


def _parse_mode(name: str) -> int | None:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return None
    digits = raw[2:] if raw.startswith("0o") else raw
    try:
        mode = int(digits, 8)
    except ValueError:
        mode = -1
    if not 0 <= mode <= 0o7777:
        _warn_once(f"mode:{name}:{raw}",
                   f"{name}={raw!r}: permissão inválida (use octal, ex.: 775). "
                   f"Regra ignorada.")
        return None
    return mode


def rules() -> dict:
    """As regras em vigor (lidas do ambiente a cada chamada: é barato e os
    testes trocam o ambiente sem recarregar módulo)."""
    return {
        "uid": _parse_id("PUID", lambda n: pwd.getpwnam(n).pw_uid),
        "gid": _parse_id("PGID", lambda n: grp.getgrnam(n).gr_gid),
        "file_mode": _parse_mode("FILE_MODE"),
        "dir_mode": _parse_mode("DIR_MODE"),
    }


def describe() -> str:
    """Resumo das regras para o log do boot."""
    r = rules()
    if all(v is None for v in r.values()):
        return "permissões: sem regra (PUID/PGID/FILE_MODE/DIR_MODE vazios)"
    def m(v):
        return "—" if v is None else f"{v:o}"
    return (f"permissões: dono {r['uid'] if r['uid'] is not None else '—'}:"
            f"{r['gid'] if r['gid'] is not None else '—'}, "
            f"arquivos {m(r['file_mode'])}, pastas {m(r['dir_mode'])}")


def apply(path, _rules: dict | None = None) -> None:
    """Aplica dono e permissão a um arquivo (FILE_MODE) ou pasta (DIR_MODE).
    Symlink: só o dono do próprio link (chmod de link não existe no Linux)."""
    r = _rules or rules()
    uid, gid = r["uid"], r["gid"]
    p = Path(path)
    try:
        is_link = p.is_symlink()
        is_dir = not is_link and p.is_dir()
        if not is_link and not p.exists():
            return
    except OSError:
        return
    if uid is not None or gid is not None:
        try:
            os.chown(p, -1 if uid is None else uid, -1 if gid is None else gid,
                     follow_symlinks=False)
        except OSError as e:
            _warn_once(f"chown:{type(e).__name__}",
                       f"chown {uid}:{gid} falhou em {p}: {e} (avisando uma vez)")
    mode = r["dir_mode"] if is_dir else r["file_mode"]
    if mode is not None and not is_link:
        try:
            os.chmod(p, mode)
        except OSError as e:
            _warn_once(f"chmod:{type(e).__name__}",
                       f"chmod {mode:o} falhou em {p}: {e} (avisando uma vez)")


def makedirs(path) -> Path:
    """mkdir -p que aplica a regra às pastas que ELE criou (as que já
    existiam ficam como estão)."""
    p = Path(path)
    novas = []
    cur = p
    while not cur.exists():
        novas.append(cur)
        if cur.parent == cur:
            break
        cur = cur.parent
    p.mkdir(parents=True, exist_ok=True)
    if novas:
        r = rules()
        for d in reversed(novas):   # de cima para baixo
            apply(d, r)
    return p
