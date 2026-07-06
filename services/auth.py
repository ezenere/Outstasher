"""Autenticacao simples por senha unica (estilo Jackett/qBittorrent).

- Uma senha, guardada com hash PBKDF2 na tabela `settings` (key MAIN_PASSWORD).
- Se nao houver senha no boot, a UI pede para criar (fluxo de "setup").
- Login troca a senha por um TOKEN de sessao (aleatorio) guardado em memoria.
  O front guarda o token no sessionStorage: fechou o navegador, cai a sessao.
  Reiniciou o servidor, cai a sessao (aceitavel para um servico simples).
- Uma API key tambem e gerada no setup (key API_KEY) para uso via header.
"""
import hashlib
import hmac
import secrets

from services import store

PASSWORD_KEY = "MAIN_PASSWORD"   # hash da senha (formato pbkdf2$iter$salt$hash)
API_KEY = "API_KEY"              # chave alternativa para chamadas via header

_PBKDF2_ITERATIONS = 200_000

# tokens de sessao validos (em memoria; some no restart, e o esperado)
_sessions: set[str] = set()


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
    _sessions.add(token)
    return token


def validate_token(token: str | None) -> bool:
    if not token:
        return False
    if token in _sessions:
        return True
    # tambem aceita a API key como token permanente (para scripts)
    api = get_api_key()
    return bool(api) and hmac.compare_digest(token, api)


def revoke_session(token: str | None):
    if token:
        _sessions.discard(token)


def revoke_all_sessions():
    """Ao trocar a senha, derruba todas as sessoes abertas."""
    _sessions.clear()
