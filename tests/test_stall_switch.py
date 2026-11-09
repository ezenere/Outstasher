"""Troca automática de torrent travado: ligar/desligar e o limite, global e
por torrent.

Pedido: a troca automática do watchdog (torrent sem progresso -> próximo
candidato) era só o STALL_TIMEOUT_MINUTES do .env. Agora a política global
mora em Configurações (o .env vira o padrão de quem nunca salvou) e cada
torrent do job — papel do filme, t<n> da série — pode herdar, ligar ou
desligar, com minutos próprios. O watchdog relê tudo a cada tick.

O watchdog roda de verdade contra um qBittorrent duplo com o torrent parado
em 40%; cada volta do watchdog anda um minuto num relógio de mentira (o
`time` dos módulos do watchdog), então "troca depois de X min" é a troca
sair com o torrent parado há X+1 min (o primeiro minuto em que passou).
"""
import asyncio
import json
import types

import pytest

import config
from services import candidates, jobs, stall_switch, store
from services.jobs import downloads, runtime, views
from services.series import pipeline


class Relogio:
    """monotonic() de mentira: anda um minuto a cada volta do watchdog (no
    intervalo entre as consultas). `on_tick(n)` roda no fim da volta n — é
    "a tela" agindo enquanto o watchdog dorme."""

    def __init__(self):
        self.t = 1000.0
        self.tick = 0
        self.on_tick = None

    def __call__(self):
        return self.t

    def avanca(self) -> float:
        self.tick += 1
        if self.on_tick:
            self.on_tick(self.tick)
        self.t += 60
        return 0


@pytest.fixture
def relogio(monkeypatch):
    r = Relogio()
    fake = types.SimpleNamespace(monotonic=r)
    for mod in (downloads, pipeline, runtime):
        monkeypatch.setattr(mod, "time", fake)
    monkeypatch.setattr(downloads, "poll_interval", r.avanca)
    monkeypatch.setattr(jobs, "poll_interval", r.avanca)
    return r


class Qbit:
    """Um torrent parado em 40%; o substituto (qualquer add depois do
    primeiro) já vem completo, e o watchdog termina. Na volta `ticks_max`
    tudo completa — o teste que não troca também termina."""

    def __init__(self, relogio, ticks_max=40):
        self.relogio = relogio
        self.torrents: dict[str, dict] = {}
        self.added: list[tuple[str, float]] = []    # (hash, relógio)
        self.visto_em = None                        # 1ª consulta do watchdog
        self.ticks_max = ticks_max
        self.state = "downloading"

    async def add(self, url, tag, save_path=None):
        h = url.split("btih:")[1].split("&")[0]
        self.added.append((h, self.relogio.t))
        t = self.torrents.setdefault(h, {"hash": h, "tags": set(), "name": h,
                                         "progress": 1.0 if self.torrents else 0.4,
                                         "added_on": len(self.added)})
        t["tags"] |= {x.strip() for x in tag.split(",")}

    async def info_by_tag(self, tag):
        if self.visto_em is None:
            self.visto_em = self.relogio.t
        if self.relogio.tick >= self.ticks_max:
            for t in self.torrents.values():
                t["progress"] = 1.0
        return [{"hash": h, "tags": ", ".join(sorted(t["tags"])), "name": t["name"],
                 "progress": t["progress"], "state": self.state,
                 "content_path": f"/dl/{h}", "added_on": t["added_on"],
                 "dlspeed": 0, "eta": 0, "num_seeds": 0, "size": 1, "completed": 0}
                for h, t in self.torrents.items() if tag in t["tags"]]

    async def remove_tag(self, h, tags):
        self.torrents[h]["tags"] -= {x.strip() for x in tags.split(",")}

    async def delete(self, h, delete_files=False):
        self.torrents.pop(h, None)

    async def files(self, h):
        return []

    def troca(self) -> int | None:
        """Há quantos minutos o torrent estava parado quando o substituto
        entrou (None = não trocou)."""
        if len(self.added) < 2:
            return None
        return int((self.added[1][1] - self.visto_em) // 60)


def _global(**kw):
    stall_switch.set({**stall_switch.defaults(), **kw})


# -------------------- política global --------------------

@pytest.mark.parametrize("env,esperado", [
    (15, {"enabled": True, "minutes": 15, "metadata_minutes": 30,
          "stalled_minutes": 120}),
    (45, {"enabled": True, "minutes": 45, "metadata_minutes": 45,
          "stalled_minutes": 120}),
    (0, {"enabled": False, "minutes": 15, "metadata_minutes": 30,
         "stalled_minutes": 120}),
])
def test_sem_nada_salvo_vale_o_env_como_antes(temp_db, monkeypatch, env, esperado):
    monkeypatch.setattr(config, "STALL_TIMEOUT_MINUTES", env)
    assert stall_switch.get() == esperado
    # os mesmos limites por estado de antes (piso de 30 min no metaDL, 2 h no
    # stalledDL, nunca abaixo do limite)
    cfg = stall_switch.get()
    if env:
        assert stall_switch.limit_minutes(cfg, "downloading") == env
        assert stall_switch.limit_minutes(cfg, "metaDL") == max(env, 30)
        assert stall_switch.limit_minutes(cfg, "stalledDL") == max(env, 120)
    else:
        assert stall_switch.limit_minutes(cfg, "stalledDL") is None


def test_global_salva_vence_o_env_e_valida(temp_db, monkeypatch):
    monkeypatch.setattr(config, "STALL_TIMEOUT_MINUTES", 15)
    out = stall_switch.set({"enabled": False, "minutes": 7,
                            "metadata_minutes": 20, "stalled_minutes": 60})
    assert stall_switch.get() == out == {"enabled": False, "minutes": 7,
                                         "metadata_minutes": 20,
                                         "stalled_minutes": 60}
    for ruim in ({"minutes": 0}, {"minutes": "dez"}, {"enabled": "sim"},
                 {"stalled_minutes": 2.5}):
        with pytest.raises(ValueError):
            stall_switch.set({**out, **ruim})
    assert stall_switch.get() == out


def test_override_vence_o_global_nos_dois_sentidos():
    ligada = {"enabled": True, "minutes": 10, "metadata_minutes": 30,
              "stalled_minutes": 120}
    desligada = dict(ligada, enabled=False)
    lim = stall_switch.limit_minutes
    assert lim(ligada, "stalledDL", "off") is None
    assert lim(desligada, "stalledDL", "on") == 120
    assert lim(desligada, "stalledDL") is None
    # minutos próprios valem em qualquer estado do torrent
    assert lim(desligada, "stalledDL", "on", 3) == 3
    assert lim(ligada, "metaDL", None, 4) == 4
    assert lim(desligada, "metaDL", None, 4) is None   # herda o "desligada"
    assert stall_switch.validate_override("inherit", None) == (None, None)
    with pytest.raises(ValueError):
        stall_switch.validate_override("talvez", None)
    with pytest.raises(ValueError):
        stall_switch.validate_override("on", 0)


# -------------------- watchdog de filme --------------------

FILME = "stf01"


def _cand(cid, title, rank):
    return candidates.new("video", cid, {
        "title": title, "tracker": "t1", "seeders": 5, "size": 100,
        "edition": None, "quality": "1080p WEB-DL", "score": 28.0,
        "magnet": f"magnet:?xt=urn:btih:{cid}", "link": None}, rank=rank)


@pytest.fixture
def filme(temp_db):
    job = {"id": FILME, "tmdb_id": 7, "language": "pt", "mode": "auto",
           "kind": "original", "status": "searching", "detail": "", "output": None,
           "movie": None, "created_at": "2026-01-01T00:00:00",
           "progress": {"video": None, "audio": None}, "search_queries": [],
           "search_candidates": [_cand("v0", "Filme Exemplo 2160p", 0),
                                 _cand("v1", "Filme Exemplo 1080p", 1)]}
    jobs._jobs[FILME] = job
    yield job
    jobs._jobs.pop(FILME, None)


def _rodar_filme(job, qbit, monkeypatch):
    monkeypatch.setattr(jobs.runtime, "_qbit", qbit)
    asyncio.run(downloads._start_download(job, None, job["search_candidates"][0]))
    asyncio.run(downloads._wait_downloads(job))
    return qbit.troca()


def test_filme_global_desligada_nao_troca(filme, relogio, monkeypatch):
    _global(enabled=False, minutes=5)
    q = Qbit(relogio, ticks_max=30)
    assert _rodar_filme(filme, q, monkeypatch) is None
    assert candidates.active(filme, "video")["id"] == "v0"


def test_filme_global_ligada_troca_depois_do_limite(filme, relogio, monkeypatch):
    """5 min: a 1ª consulta vê 40% (o relógio começa ali); sem progresso,
    troca na primeira volta em que passou de 5 min parado."""
    _global(enabled=True, minutes=5)
    q = Qbit(relogio)
    assert _rodar_filme(filme, q, monkeypatch) == 6
    assert candidates.active(filme, "video")["id"] == "v1"


def test_filme_limite_do_estado_sem_fontes(filme, relogio, monkeypatch):
    """stalledDL espera o campo próprio (aqui 12 min), não o limite base."""
    _global(enabled=True, minutes=5, stalled_minutes=12)
    q = Qbit(relogio)
    q.state = "stalledDL"
    assert _rodar_filme(filme, q, monkeypatch) == 13


def test_filme_override_desligada_vence_global_ligada(filme, relogio, monkeypatch):
    _global(enabled=True, minutes=5)
    filme["stall_mode_video"] = "off"
    q = Qbit(relogio, ticks_max=30)
    assert _rodar_filme(filme, q, monkeypatch) is None


def test_filme_override_ligada_vence_global_desligada(filme, relogio, monkeypatch):
    _global(enabled=False, minutes=30)
    filme["stall_mode_video"] = "on"
    filme["stall_minutes_video"] = 3
    q = Qbit(relogio)
    assert _rodar_filme(filme, q, monkeypatch) == 4


def test_filme_mudar_na_tela_vale_no_proximo_tick(filme, relogio, monkeypatch):
    """Desligada no torrent e parado há 9 min; o usuário volta para "herdar"
    (global 5 min) — a troca sai na consulta seguinte."""
    _global(enabled=True, minutes=5)
    filme["stall_mode_video"] = "off"
    vistos = []

    def tela(tick):
        if tick == 10:
            # o que a tela mostra antes: parado há 9 min, sem troca prevista
            vistos.append(views.stall_view(filme)["torrents"]["video"])
            jobs.set_stall_switch(FILME, "inherit", kind="video")
    relogio.on_tick = tela
    q = Qbit(relogio)
    assert _rodar_filme(filme, q, monkeypatch) == 10
    assert vistos == [{"idle_s": 9 * 60, "limit_s": None, "paused": False,
                       "no_backup": False}]
    assert "stall_mode_video" not in filme
    # o watchdog acabou: o relógio sai da tela
    assert runtime.stall_clocks(filme) == {}


def test_filme_tela_mostra_quando_vai_trocar(filme, relogio, monkeypatch):
    _global(enabled=True, minutes=15)
    vistos = []

    def tela(tick):
        if tick == 10:
            vistos.append(views.stall_view(filme))
    relogio.on_tick = tela
    q = Qbit(relogio)
    _rodar_filme(filme, q, monkeypatch)
    v = vistos[0]
    assert v["global"]["enabled"] is True and v["global"]["minutes"] == 15
    # parado há 9 min, troca aos 15 (a tela diz "troca em 6 min")
    assert v["torrents"]["video"] == {"idle_s": 9 * 60, "limit_s": 15 * 60,
                                      "paused": False, "no_backup": False}


# -------------------- watchdog de série --------------------

SERIE = "sts01"
SHARED = f"dl-{SERIE}"
EP = "Serie Exemplo S01E01 1080p WEB-DL"
EP_RESERVA = "Serie Exemplo S01E01 720p WEB-DL"


def _mag(h):
    return f"magnet:?xt=urn:btih:{h * 40}"


def _scand(title, h):
    return {"id": None, "title": title, "tracker": "x", "seeders": 5, "size": 900,
            "quality": "1080p WEB-DL", "coverage": None, "score": 1.0, "tier": 28,
            "magnet": _mag(h), "link": None, "infohash": None, "files": None}


@pytest.fixture
def serie(temp_db, monkeypatch):
    job = {
        "id": SERIE, "media_type": "tv", "tmdb_id": 4242, "language": "pt",
        "mode": "auto", "kind": "original", "status": "downloading", "detail": "",
        "movie": {"original_title": "Serie Exemplo", "localized_title": "Serie Exemplo",
                  "english_title": None, "year": "2008", "poster": None,
                  "overview": None, "original_language": "en", "id": 4242},
        "request": {"seasons": [1], "episodes": {}}, "known_seasons": [1],
        "episodes": {"S01E01": {"season": 1, "episode": 1, "name": "Ep 1",
                                "air_date": "2008-01-20", "runtime": 48,
                                "state": "downloading", "src": {}, "output": None,
                                "error": None}},
        "torrents": [{"n": 0, "role": "original", "tag": f"{SHARED}-t0",
                      "title": EP, "tracker": "x", "seeders": 5, "size": 900,
                      "quality": "1080p WEB-DL", "coverage_label": "S01E01",
                      "magnet": _mag("a"), "link": None, "infohash": None,
                      "files_count": None, "coverage": ["S01E01"],
                      "state": "downloading", "hash": None, "progress": None,
                      "selected_files": None, "content_path": None}],
        "awaiting": None, "order_map": None, "report": None, "progress": {},
        "output": None, "torrent_save_path": "", "search_queries": [],
        "search_candidates": [], "created_at": "2026-11-08T00:00:00",
    }
    candidates.set_series(job, "original", {"S01E01": [_scand(EP, "a"),
                                                       _scand(EP_RESERVA, "b")]})
    jobs._jobs[SERIE] = job

    async def nada(job):
        return None
    # depois do download vem o merge: fora do escopo daqui
    monkeypatch.setattr(pipeline, "_resolve_episode_files", nada)
    monkeypatch.setattr(pipeline, "_after_download", nada)
    yield job
    jobs._jobs.pop(SERIE, None)
    pipeline._pools.pop(SERIE, None)


def _rodar_serie(job, qbit, monkeypatch):
    monkeypatch.setattr(jobs, "_qbit", qbit)
    asyncio.run(qbit.add(job["torrents"][0]["magnet"], f"{SHARED},{SHARED}-t0"))
    asyncio.run(pipeline._wait_downloads(job))
    return qbit.troca()


def test_serie_global_ligada_troca_e_desligada_nao(serie, relogio, monkeypatch):
    _global(enabled=False, minutes=5)
    assert _rodar_serie(serie, Qbit(relogio, ticks_max=30), monkeypatch) is None
    assert serie["torrents"][0]["title"] == EP


def test_serie_global_ligada_troca_depois_do_limite(serie, relogio, monkeypatch):
    _global(enabled=True, minutes=5)
    assert _rodar_serie(serie, Qbit(relogio), monkeypatch) == 6
    assert serie["torrents"][0]["title"] == EP_RESERVA


def test_serie_override_do_torrent_vence_o_global(serie, relogio, monkeypatch):
    _global(enabled=True, minutes=5)
    jobs.set_stall_switch(SERIE, "off", n=0)
    assert _rodar_serie(serie, Qbit(relogio, ticks_max=30), monkeypatch) is None


def test_serie_override_ligada_com_minutos_proprios(serie, relogio, monkeypatch):
    _global(enabled=False, minutes=30)
    jobs.set_stall_switch(SERIE, "on", 3, n=0)
    vistos = []

    def tela(tick):
        if tick == 3:
            vistos.append(views.stall_view(serie)["torrents"]["0"])
    relogio.on_tick = tela
    assert _rodar_serie(serie, Qbit(relogio), monkeypatch) == 4
    assert vistos == [{"idle_s": 120, "limit_s": 180, "paused": False,
                       "no_backup": False}]
    # o override é do torrent t0 do plano: segue no substituto
    assert serie["torrents"][0]["stall_mode"] == "on"


def test_override_valida_o_alvo(serie, filme):
    with pytest.raises(ValueError, match="t7"):
        jobs.set_stall_switch(SERIE, "on", n=7)
    with pytest.raises(ValueError, match="não baixa"):
        jobs.set_stall_switch(FILME, "on", kind="audio")
    with pytest.raises(ValueError, match="modo"):
        jobs.set_stall_switch(FILME, "sempre", kind="video")


# -------------------- persistência do override --------------------

def _data(jid) -> dict:
    with store._lock:
        return json.loads(store._conn.execute(
            "SELECT data FROM jobs WHERE id = ?", (jid,)).fetchone()[0] or "{}")


def test_override_do_filme_round_trip_em_colunas(filme):
    filme["status"] = "downloading"
    jobs.set_stall_switch(FILME, "off", kind="video")
    jobs.set_stall_switch(FILME, "on", 7, kind="video")
    store.upsert_job(filme)
    back = store.get_job(FILME)
    assert back["stall_mode_video"] == "on" and back["stall_minutes_video"] == 7
    assert "stall_mode_audio" not in back           # nunca mexido: herda
    assert not any(k.startswith("stall_") for k in _data(FILME))
    with store._lock:
        row = store._conn.execute(
            "SELECT stall_mode_video, stall_minutes_video, stall_mode_audio "
            "FROM jobs WHERE id = ?", (FILME,)).fetchone()
    assert tuple(row) == ("on", 7, None)
    # voltar a herdar tira as chaves — e o banco devolve o job sem elas
    jobs.set_stall_switch(FILME, "inherit", kind="video")
    store.upsert_job(filme)
    back = store.get_job(FILME)
    assert not any(k.startswith("stall_") for k in back)


def test_override_da_serie_round_trip_em_colunas(serie):
    jobs.set_stall_switch(SERIE, "off", 9, n=0)
    store.upsert_job(serie)
    t = store.get_job(SERIE)["torrents"][0]
    assert t["stall_mode"] == "off" and t["stall_minutes"] == 9
    with store._lock:
        row = store._conn.execute(
            "SELECT stall_mode, stall_minutes, extra FROM job_torrents "
            "WHERE job_id = ? AND n = 0", (SERIE,)).fetchone()
    assert tuple(row) == ("off", 9, None)
    jobs.set_stall_switch(SERIE, None, n=0)
    store.upsert_job(serie)
    t = store.get_job(SERIE)["torrents"][0]
    assert "stall_mode" not in t and "stall_minutes" not in t


def test_banco_antigo_ganha_as_colunas(temp_db):
    """job_torrents de antes do override: o init acrescenta as colunas."""
    with store._lock:
        cols = {r[1] for r in store._conn.execute("PRAGMA table_info(job_torrents)")}
        jcols = {r[1] for r in store._conn.execute("PRAGMA table_info(jobs)")}
    assert {"stall_mode", "stall_minutes"} <= cols
    assert {"stall_mode_video", "stall_minutes_audio"} <= jcols


# -------------------- API --------------------

def _client(monkeypatch):
    from fastapi.testclient import TestClient
    import main
    monkeypatch.setattr(main.auth, "is_password_set", lambda: True)
    monkeypatch.setattr(main.auth, "validate_token", lambda tok: True)
    return TestClient(main.app)


def test_api_global_e_por_torrent(filme, monkeypatch):
    c = _client(monkeypatch)
    monkeypatch.setattr(config, "STALL_TIMEOUT_MINUTES", 15)
    r = c.get("/api/stall-switch").json()
    assert r["config"] == r["defaults"] and r["config"]["minutes"] == 15
    r = c.put("/api/stall-switch", json={"config": {**r["config"], "minutes": 8}})
    assert r.status_code == 200 and stall_switch.get()["minutes"] == 8
    assert c.put("/api/stall-switch",
                 json={"config": {"minutes": -1}}).status_code == 400
    r = c.put(f"/api/jobs/{FILME}/stall-switch",
              json={"kind": "video", "mode": "off"})
    assert r.status_code == 200 and r.json()["stall_mode_video"] == "off"
    assert c.put(f"/api/jobs/{FILME}/stall-switch",
                 json={"kind": "video", "mode": "x"}).status_code == 400
