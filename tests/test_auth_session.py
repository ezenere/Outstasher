"""Sessão da UI por cookie (como o SID do qBittorrent).

- o login grava um cookie de SESSÃO (sem validade: o navegador apaga ao
  fechar), HttpOnly e SameSite=Strict; toda aba manda o mesmo cookie;
- a sessão sobrevive a reinício do servidor (hash no banco, não o token);
- expira depois de SESSION_IDLE_S sem uso; logout e troca de senha derrubam;
- a API key continua valendo pelo header (scripts).
"""
import pytest

from services import auth


@pytest.fixture
def client(temp_db):
    from fastapi.testclient import TestClient
    import main
    _reinicia_auth()
    auth.set_password("teste123")
    return TestClient(main.app)


def _reinicia_auth():
    """Estado em memória do auth como num processo recém-iniciado."""
    auth._sessions.clear()
    auth._persisted.clear()
    auth._loaded = False


def _login(c, senha="teste123"):
    r = c.post("/api/auth/login", json={"password": senha})
    assert r.status_code == 200, r.text
    return r


def test_login_grava_cookie_de_sessao(client):
    r = _login(client)
    assert "token" not in r.json()          # o JS não vê o token
    set_cookie = r.headers["set-cookie"].lower()
    assert set_cookie.startswith("outstasher_sid=")
    assert "httponly" in set_cookie and "samesite=strict" in set_cookie
    assert "max-age" not in set_cookie and "expires" not in set_cookie
    assert client.get("/api/jobs/summary").status_code == 200
    assert client.get("/api/auth/status").json()["authenticated"] is True


def test_sem_cookie_nao_entra(client):
    from fastapi.testclient import TestClient
    import main
    _login(client)
    outro = TestClient(main.app)            # outro navegador: sem o cookie
    assert outro.get("/api/jobs/summary").status_code == 401
    assert outro.get("/api/auth/status").json()["authenticated"] is False


def test_sessao_sobrevive_a_reinicio(client, temp_db):
    _login(client)
    token = client.cookies.get("outstasher_sid")
    with temp_db._lock:
        hashes = [h for (h,) in temp_db._conn.execute(
            "SELECT token_hash FROM auth_sessions")]
    assert hashes == [auth._digest(token)]  # só o hash vai ao banco
    _reinicia_auth()
    assert client.get("/api/jobs/summary").status_code == 200


def test_expira_sem_uso(client, monkeypatch):
    _login(client)
    agora = auth.time.time()
    monkeypatch.setattr(auth.time, "time", lambda: agora + auth.SESSION_IDLE_S - 60)
    assert client.get("/api/jobs/summary").status_code == 200   # uso renova
    monkeypatch.setattr(auth.time, "time",
                        lambda: agora + 2 * auth.SESSION_IDLE_S - 120)
    assert client.get("/api/jobs/summary").status_code == 200
    monkeypatch.setattr(auth.time, "time",
                        lambda: agora + 3 * auth.SESSION_IDLE_S)
    assert client.get("/api/jobs/summary").status_code == 401
    _reinicia_auth()                        # e não volta do banco
    assert client.get("/api/jobs/summary").status_code == 401


def test_logout_derruba_a_sessao(client):
    _login(client)
    token = client.cookies.get("outstasher_sid")
    r = client.post("/api/auth/logout")
    assert r.status_code == 200
    assert client.get("/api/jobs/summary").status_code == 401
    client.cookies.set("outstasher_sid", token)  # cookie velho guardado
    assert client.get("/api/jobs/summary").status_code == 401


def test_troca_de_senha_derruba_as_outras(client):
    from fastapi.testclient import TestClient
    import main
    outro = TestClient(main.app)
    _login(client)
    _login(outro)
    r = client.post("/api/auth/change-password",
                    json={"current_password": "teste123", "new_password": "nova123"})
    assert r.status_code == 200, r.text
    assert client.get("/api/jobs/summary").status_code == 200  # quem trocou fica
    assert outro.get("/api/jobs/summary").status_code == 401


def test_api_key_pelo_header(client):
    from fastapi.testclient import TestClient
    import main
    script = TestClient(main.app)
    h = {"Authorization": f"Bearer {auth.get_api_key()}"}
    assert script.get("/api/jobs/summary", headers=h).status_code == 200
    assert script.get("/api/jobs/summary",
                      headers={"Authorization": "Bearer errado"}).status_code == 401
