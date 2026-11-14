"""Autenticacao simples por senha unica (estilo Jackett/qBittorrent).

- Uma senha, guardada com hash PBKDF2 na tabela `settings` (key MAIN_PASSWORD).
- Se nao houver senha no boot, a UI pede para criar (fluxo de "setup").
- Login troca a senha por um TOKEN de sessao (aleatorio), entregue num COOKIE
  de sessao (HttpOnly, sem validade): vale em todas as abas e o navegador
  apaga ao fechar — como o SID do qBittorrent. Do lado do servidor a sessao
  expira apos SESSION_IDLE_S sem uso (aba aberta faz polling e a mantem
  viva) e sobrevive a reinicio/deploy: o hash do token fica no banco.
- Uma API key tambem e gerada no setup (key API_KEY) para uso via header.
"""
import hashlib
import hmac
import secrets
import threading
import time

from services import store

PASSWORD_KEY = "MAIN_PASSWORD"   # hash da senha (formato pbkdf2$iter$salt$hash)
API_KEY = "API_KEY"              # chave alternativa para chamadas via header

_PBKDF2_ITERATIONS = 200_000

SESSION_IDLE_S = 24 * 3600   # sessao sem uso por isto: expira
_TOUCH_EVERY_S = 300         # ultimo uso vai ao banco no maximo a cada 5 min

# sessoes validas: {sha256 do token: ultimo uso} — espelho do banco, carregado
# no primeiro uso (o polling da UI nao pode ler o banco a cada requisicao)
_sessions: dict[str, float] = {}
_persisted: dict[str, float] = {}   # ultimo uso que esta no banco
_loaded = False
_mu = threading.Lock()


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _ensure_loaded():
    global _loaded
    if _loaded:
        return
    now = time.time()
    try:
        store.delete_sessions(older_than=now - SESSION_IDLE_S)
        rows = store.load_sessions()
    except Exception:  # noqa: BLE001 — sem banco (testes): so memoria
        rows = {}
    _sessions.update(rows)
    _persisted.update(rows)
    _loaded = True


# -------------------- hashing da senha --------------------

def _hash_password(password: str, *, salt: bytes | None = None,
                   iterations: int = _PBKDF2_ITERATIONS) -> str:
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"pbkdf2${iterations}${salt.hex()}${dk.hex()}"


def _check_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt_hex, hash_hex = stored.split("$")
        if algo != "pbkdf2":
            return False
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt_hex), int(iters))
        return hmac.compare_digest(dk.hex(), hash_hex)
    except (ValueError, TypeError):
        return False


# -------------------- estado da senha --------------------

def is_password_set() -> bool:
    return bool(store.get_setting(PASSWORD_KEY))


def set_password(password: str):
    """Cria/troca a senha. Gera a API key se ainda nao existir."""
    store.set_setting(PASSWORD_KEY, _hash_password(password))
    if not store.get_setting(API_KEY):
        store.set_setting(API_KEY, secrets.token_urlsafe(24))


def verify_password(password: str) -> bool:
    stored = store.get_setting(PASSWORD_KEY)
    return bool(stored) and _check_password(password, stored)


def get_api_key() -> str | None:
    return store.get_setting(API_KEY)


# -------------------- sessoes --------------------

def create_session() -> str:
    token = secrets.token_urlsafe(32)
    h, now = _digest(token), time.time()
    with _mu:
        _ensure_loaded()
        _sessions[h] = now
        _persisted[h] = now
    store.save_session(h, now)
    return token


def _session_alive(token: str) -> bool:
    h, now = _digest(token), time.time()
    with _mu:
        _ensure_loaded()
        last = _sessions.get(h)
        if last is None:
            return False
        if now - last > SESSION_IDLE_S:
            _sessions.pop(h, None)
            _persisted.pop(h, None)
            expired = True
        else:
            expired = False
            _sessions[h] = now
            touch = now - _persisted.get(h, 0.0) >= _TOUCH_EVERY_S
            if touch:
                _persisted[h] = now
    if expired:
        store.delete_sessions([h])
        return False
    if touch:
        store.touch_session(h, now)
    return True


def validate_token(token: str | None) -> bool:
    if not token:
        return False
    if _session_alive(token):
        return True
    # tambem aceita a API key como token permanente (para scripts)
    api = get_api_key()
    return bool(api) and hmac.compare_digest(token, api)


def revoke_session(token: str | None):
    if not token:
        return
    h = _digest(token)
    with _mu:
        _sessions.pop(h, None)
        _persisted.pop(h, None)
    store.delete_sessions([h])


def revoke_all_sessions():
    """Ao trocar a senha, derruba todas as sessoes abertas."""
    with _mu:
        _ensure_loaded()
        _sessions.clear()
        _persisted.clear()
    store.delete_sessions()
