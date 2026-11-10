"""Busca programada e contínua.

- job PROGRAMADO (status "scheduled"): filme/episódio com data e hora, ou
  episódio futuro esperando o dia do lançamento — um job por data;
- busca CONTÍNUA: a busca que não dá em nada (nada encontrado, nada na
  qualidade mínima, nenhum par do mesmo corte) agenda a próxima tentativa,
  com a escolha (lista + magnet próprio) aberta enquanto isso;
- busca INCREMENTAL: a nova tentativa mantém o que a anterior achou e o
  tracker não desmentiu (tracker que falhou não apaga a lista);
- qualidade mínima por lado (original e dublado), só na escolha automática.

Jackett/TMDB são funções falsas; o qBittorrent é um duplo.
"""
import asyncio
from datetime import date, datetime, timedelta

import pytest

from services import candidates, jackett, jobs, search_retry, selector, tmdb
from services.jobs import actions, movies, schedule, views
from services.series import pipeline


def _hit(title, tracker="t1", seeders=10, size=2000):
    return {"title": title, "seeders": seeders, "size": size,
            "magnet": f"magnet:?xt=urn:btih:{abs(hash(title)) % 10 ** 12:040x}",
            "link": None, "tracker": tracker, "tracker_id": tracker,
            "files": None, "infohash": None}


class FakeQbit:
    def __init__(self):
        self.added: list[tuple[str, str]] = []

    async def add(self, url, tag, save_path=None):
        self.added.append((url, tag))

    async def info_by_tag(self, tag):
        return []


@pytest.fixture
def indexador(monkeypatch):
    por_query: dict[str, list[dict]] = {}

    async def fake_search(query, indexer="all", categories=None):
        return list(por_query.get(query, []))
    monkeypatch.setattr(jackett, "search", fake_search)
    return por_query


@pytest.fixture
def qbit(monkeypatch):
    q = FakeQbit()
    monkeypatch.setattr(jobs.runtime, "_qbit", q)
    monkeypatch.setattr(jobs, "_qbit", q)
    return q


@pytest.fixture
def relogio(monkeypatch):
    """Relógio da programação controlável (search_retry.now)."""
    agora = {"t": datetime(2026, 11, 10, 12, 0, 0)}
    monkeypatch.setattr(search_retry, "now", lambda: agora["t"])
    return agora


@pytest.fixture
def parar_no_download(monkeypatch):
    chegou = []

    async def watchdog(job):
        chegou.append(job["status"])
    monkeypatch.setattr(movies, "_run_from_download", watchdog)
    monkeypatch.setattr(pipeline, "_wait_downloads", watchdog)
    return chegou


@pytest.fixture(autouse=True)
def _limpa():
    yield
    for jid in list(jobs._jobs):
        jobs._jobs.pop(jid, None)
        pipeline._pools.pop(jid, None)


async def _drain():
    """Espera as tasks do pipeline (inclusive as que elas criam)."""
    for _ in range(10):
        pend = [t for t in jobs._tasks.values() if not t.done()]
        if not pend:
            return
        await asyncio.gather(*pend, return_exceptions=True)


# ============================== qualidade mínima ==============================

@pytest.mark.parametrize("title,piso,ok", [
    ("Filme 2014 1080p WEB-DL", {"res": "1080p", "source": None}, True),
    ("Filme 2014 720p WEB-DL", {"res": "1080p", "source": None}, False),
    ("Filme 2014 2160p BluRay", {"res": "1080p", "source": "webdl"}, True),
    ("Filme 2014 1080p WEBRip", {"res": None, "source": "webdl"}, False),
    ("Filme 2014 Dublado", {"res": "720p", "source": None}, False),   # sem resolução
    ("Filme 2014 4K AI Upscale WEB-DL", {"res": "1080p", "source": None}, False),
    ("Filme 2014 1080p HDTV", {"res": None, "source": "webrip"}, False),
    ("Filme 2014 qualquer", None, True),
])
def test_meets_minimum(title, piso, ok):
    assert selector.meets_minimum(title, piso) is ok


def test_validate_min_quality():
    assert selector.validate_min_quality({"res": None, "source": None}) is None
    assert selector.validate_min_quality({"res": "720p"}) == {"res": "720p", "source": None}
    with pytest.raises(ValueError):
        selector.validate_min_quality({"res": "999p"})
    with pytest.raises(ValueError):
        selector.validate_min_quality({"source": "cam"})


# ============================== configuração ==============================

def test_config_global_padrao_e_validacao(temp_db):
    assert search_retry.get() == {"enabled": True, "minutes": 360, "release_time": "06:00"}
    assert search_retry.set({"enabled": False, "minutes": 90, "release_time": "21:30"}) == {
        "enabled": False, "minutes": 90, "release_time": "21:30"}
    assert search_retry.get()["minutes"] == 90
    for ruim in ({"minutes": 0}, {"release_time": "25:00"}, {"enabled": "sim"}):
        with pytest.raises(ValueError):
            search_retry.set(ruim)


def test_override_por_job(temp_db):
    assert search_retry.enabled_for({}) is True
    assert search_retry.enabled_for({"retry_mode": "off"}) is False
    search_retry.set({"enabled": False})
    assert search_retry.enabled_for({}) is False
    assert search_retry.enabled_for({"retry_mode": "on"}) is True


def test_datas():
    assert search_retry.release_at("2026-12-01", "21:15") == datetime(2026, 12, 1, 21, 15)
    assert search_retry.release_at(None, "06:00") is None
    # com fuso vira o horário local sem fuso (o do servidor)
    dt = search_retry.parse_when("2026-12-01T20:00:00+00:00")
    assert dt.tzinfo is None
    with pytest.raises(ValueError):
        search_retry.parse_when("amanhã")


# ============================== filme ==============================

@pytest.fixture
def filme_tmdb(temp_db, tmp_path, monkeypatch):
    temp_db.add_destination("Filmes", str(tmp_path / "out"), True)

    async def fake_details(tmdb_id, lang):
        return {"id": tmdb_id, "original_title": "Filme Exemplo",
                "localized_title": "Filme Teste", "english_title": None,
                "original_language": "en", "year": "2014", "overview": None,
                "poster": "p.jpg"}
    monkeypatch.setattr(tmdb, "details", fake_details)


def _criar_filme(**kw):
    async def go():
        out = await movies.create(7, "pt", **kw)
        await _drain()
        # terminal sai da memória: o banco tem o estado final
        return jobs._jobs.get(out["id"]) or jobs.get_job(out["id"])
    return asyncio.run(go())


def test_filme_programado_espera_a_hora_e_carrega_o_titulo(filme_tmdb, indexador, relogio, qbit):
    job = _criar_filme(scheduled_at="2026-11-12T20:00")
    assert job["status"] == "scheduled"
    assert job["next_search_at"] == "2026-11-12T20:00:00"
    assert job["detail"] == "Programado para 12/11 às 20:00"
    assert job["movie"]["original_title"] == "Filme Exemplo"   # já na lista
    assert indexador == {} and qbit.added == []
    # antes da hora o relógio não dispara
    assert schedule.tick() == 0
    # na hora: busca (e, sem nada no indexador, agenda a próxima tentativa)
    relogio["t"] = datetime(2026, 11, 12, 20, 0, 5)

    async def go():
        assert schedule.tick() == 1
        await _drain()
    asyncio.run(go())
    assert job["searched_at"] == "2026-11-12T20:00:05"
    assert job["status"] == "scheduled"
    assert job["next_search_at"] == "2026-11-13T02:00:05"     # +6 h
    assert job["detail"].startswith("Nenhum torrent encontrado para o dublado e o original")


def test_filme_sem_resultado_agenda_e_a_escolha_segue_aberta(
        filme_tmdb, indexador, relogio, qbit, parar_no_download):
    indexador["Filme Exemplo 2014"] = [_hit("Filme Exemplo 2014 1080p WEB-DL")]
    job = _criar_filme()
    assert job["status"] == "scheduled"
    assert "nova busca 10/11 às 18:00" in job["detail"]
    busca = jobs.get_job(job["id"])["search"]
    assert busca["audio"] == [] and len(busca["video"]) == 1
    # escolher no programado: magnet próprio do dublado + o original da lista
    magnet = "magnet:?xt=urn:btih:" + "f" * 40

    async def go():
        await actions.select(job["id"], None, busca["video"][0]["id"],
                             custom={"audio": {"url": magnet, "title": "Filme Teste 2014 Dublado"}})
        await _drain()
    asyncio.run(go())
    assert parar_no_download == ["downloading"]
    assert "next_search_at" not in job and "search_miss" not in job


def test_filme_busca_incremental(filme_tmdb, indexador, relogio, qbit):
    """t1 respondeu sem o A (saiu do tracker), t2 não respondeu nada (falhou):
    o A sai, o B fica, o C novo entra."""
    indexador["Filme Teste 2014"] = [
        _hit("Filme Teste 2014 1080p WEB-DL Dublado A", "t1"),
        _hit("Filme Teste 2014 720p WEB-DL Dublado B", "t2")]
    job = _criar_filme(kind="dubbed", mode="manual")
    assert job["status"] == "awaiting"
    indexador["Filme Teste 2014"] = [_hit("Filme Teste 2014 2160p WEB-DL Dublado C", "t1")]

    async def go():
        await actions.research(job["id"])
        await _drain()
    asyncio.run(go())
    titulos = [c["title"] for c in jobs.get_job(job["id"])["search"]["audio"]]
    assert titulos == ["Filme Teste 2014 2160p WEB-DL Dublado C",
                       "Filme Teste 2014 720p WEB-DL Dublado B"]


def test_filme_torrent_sem_seeders_sai_da_lista(filme_tmdb, indexador, relogio, qbit):
    indexador["Filme Teste 2014"] = [_hit("Filme Teste 2014 1080p WEB-DL Dublado", "t2")]
    job = _criar_filme(kind="dubbed", mode="manual")
    indexador["Filme Teste 2014"] = [_hit("Filme Teste 2014 1080p WEB-DL Dublado", "t2", seeders=0)]

    async def go():
        await actions.research(job["id"])
        await _drain()
    asyncio.run(go())
    assert jobs.get_job(job["id"])["search"]["audio"] == []


def test_filme_qualidade_minima(filme_tmdb, indexador, relogio, qbit, parar_no_download):
    indexador["Filme Exemplo 2014"] = [_hit("Filme Exemplo 2014 1080p WEB-DL")]
    job = _criar_filme(kind="original",
                       min_quality={"video": {"res": "2160p", "source": None}})
    assert job["status"] == "scheduled"
    assert job["detail"].startswith("Nenhum torrent na qualidade mínima para o original")
    # o candidato abaixo do piso continua na lista para a escolha à mão
    assert len(jobs.get_job(job["id"])["search"]["video"]) == 1
    # apareceu um 4K: a próxima tentativa baixa sozinha
    indexador["Filme Exemplo 2014"].append(_hit("Filme Exemplo 2014 2160p WEB-DL", "t1"))
    relogio["t"] += timedelta(hours=7)

    async def go():
        assert schedule.tick() == 1
        await _drain()
    asyncio.run(go())
    assert parar_no_download == ["downloading"]
    assert candidates.active(job, "video")["title"] == "Filme Exemplo 2014 2160p WEB-DL"


def test_qualidade_minima_ignorada_no_manual(filme_tmdb):
    job = _criar_filme(mode="manual", scheduled_at="2030-01-01T00:00",
                       min_quality={"video": {"res": "2160p"}})
    assert "min_quality" not in job


def test_busca_continua_desligada_no_job_abre_a_escolha(filme_tmdb, indexador, relogio, qbit):
    job = _criar_filme(retry_mode="off")
    assert job["status"] == "awaiting"
    assert job["search_miss"].startswith("Nenhum torrent encontrado")
    # ligar no job parado na escolha agenda a próxima tentativa
    schedule.set_retry_mode(job["id"], "on")
    assert job["status"] == "scheduled" and job["next_search_at"]
    # desligar de novo volta para a escolha
    schedule.set_retry_mode(job["id"], "off")
    assert job["status"] == "awaiting" and "next_search_at" not in job


def test_reprogramar_e_buscar_agora(filme_tmdb, indexador, relogio, qbit):
    job = _criar_filme(scheduled_at="2026-11-20T08:00")
    schedule.reschedule(job["id"], "2026-11-15T09:30")
    assert job["next_search_at"] == "2026-11-15T09:30:00"
    assert job["detail"] == "Programado para 15/11 às 09:30"

    async def go():
        schedule.search_now(job["id"])
        await _drain()
    asyncio.run(go())
    assert job["searched_at"] == "2026-11-10T12:00:00"
    # depois de buscar, a data não se edita mais (só a próxima tentativa corre)
    with pytest.raises(ValueError, match="ainda não começou"):
        schedule.reschedule(job["id"], "2026-11-16T09:30")


def test_restart_durante_a_busca_reagenda(filme_tmdb, relogio):
    job = {"id": "rs1", "tmdb_id": 7, "language": "pt", "mode": "auto",
           "kind": "both", "status": "searching", "detail": "", "output": None,
           "movie": None, "created_at": "2026-11-08T00:00:00",
           "progress": {"video": None, "audio": None},
           "search_queries": [], "search_candidates": []}
    jobs._jobs[job["id"]] = job
    actions.resume_pending()
    assert job["status"] == "scheduled"
    assert job["next_search_at"] == "2026-11-10T12:00:00"


def test_grupo_programados_na_listagem(filme_tmdb, indexador, relogio):
    a = _criar_filme(scheduled_at="2026-11-20T08:00")
    b = _criar_filme(scheduled_at="2026-11-15T08:00")
    page = views.list_group("scheduled")
    assert [j["id"] for j in page["items"]] == [b["id"], a["id"]]   # próximo primeiro
    assert page["items"][0]["next_search_at"] == "2026-11-15T08:00:00"
    assert views.list_group("active")["items"] == []
    assert views.counts()["scheduled"] == 2


# ============================== série ==============================

HOJE = date.today()


def _d(dias):
    return (HOJE + timedelta(days=dias)).isoformat()


@pytest.fixture
def serie_tmdb(temp_db, tmp_path, monkeypatch):
    temp_db.add_destination("Séries", str(tmp_path / "tv"), True, "tv")
    eps = {1: [(1, "2008-01-20"), (2, _d(3)), (3, _d(3)), (4, _d(10)), (5, None)]}

    async def fake_details(tv_id, lang):
        return {"id": tv_id, "original_title": "Serie Exemplo",
                "localized_title": "Serie Teste", "english_title": None,
                "original_language": "en", "year": "2008", "overview": None,
                "poster": None, "seasons": [{"season": 1, "air_date": "2008-01-20"}]}

    async def fake_season(tv_id, season):
        return {"season": season, "name": f"T{season}",
                "episodes": [{"episode": e, "name": f"Ep {e}", "air_date": d,
                              "runtime": 45, "overview": None} for e, d in eps[season]]}

    async def fake_groups(tv_id):
        return []
    monkeypatch.setattr(tmdb, "tv_details", fake_details)
    monkeypatch.setattr(tmdb, "tv_season", fake_season)
    monkeypatch.setattr(tmdb, "tv_episode_groups", fake_groups)
    return eps


def _criar_serie(**kw):
    async def go():
        out = await pipeline.create_series(4242, "pt", **kw)
        await _drain()
        return out["id"]
    return asyncio.run(go())


def _series_jobs():
    return sorted((j for j in jobs._jobs.values() if j.get("media_type") == "tv"),
                  key=lambda j: sorted(j["episodes"]))


def test_serie_futuros_viram_um_job_por_data(serie_tmdb, indexador, qbit, parar_no_download):
    indexador["Serie Exemplo S01"] = [_hit("Serie Exemplo S01E01 1080p WEB-DL")]
    indexador["Serie Teste S01"] = [_hit("Serie Teste S01E01 1080p WEB-DL Dublado")]
    pai = _criar_serie(seasons=[1], schedule_future=True, release_time="21:00")
    por_eps = {tuple(sorted(j["episodes"])): j for j in _series_jobs()}
    assert set(por_eps) == {("S01E01",), ("S01E02", "S01E03"), ("S01E04",), ("S01E05",)}
    # o pai ficou com o lançado e seguiu para o download
    assert por_eps[("S01E01",)]["id"] == pai
    assert parar_no_download == ["downloading"]
    assert jobs._jobs[pai]["request"] == {"seasons": [], "episodes": {1: [1]}}
    # cada data vira um programado que busca no dia, na hora escolhida
    j23 = por_eps[("S01E02", "S01E03")]
    assert j23["status"] == "scheduled" and j23["wait_release"]
    assert j23["next_search_at"] == f"{_d(3)}T21:00:00"
    assert j23["request"] == {"seasons": [], "episodes": {1: [2, 3]}}
    assert por_eps[("S01E04",)]["next_search_at"] == f"{_d(10)}T21:00:00"
    # sem data no TMDB: confere de novo depois do intervalo
    j5 = por_eps[("S01E05",)]
    assert j5["status"] == "scheduled" and "sem data no TMDB" in j5["detail"]
    # o pedido dos filhos é só deles: reler o TMDB não divide de novo
    assert all("schedule_future" not in j for j in jobs._jobs.values())


def test_serie_so_futuros_o_job_fica_com_a_primeira_data(serie_tmdb, indexador):
    pai = _criar_serie(episodes={1: [4, 2]}, schedule_future=True)
    todos = _series_jobs()
    assert len(todos) == 2
    job = jobs._jobs[pai]
    assert sorted(job["episodes"]) == ["S01E02"]
    assert job["status"] == "scheduled"
    assert job["next_search_at"] == f"{_d(3)}T06:00:00"     # hora global


def test_serie_estreia_hoje_espera_a_hora(serie_tmdb, indexador, monkeypatch):
    serie_tmdb[1].append((6, HOJE.isoformat()))
    monkeypatch.setattr(search_retry, "now",
                        lambda: datetime.combine(HOJE, datetime.min.time()).replace(hour=3))
    pai = _criar_serie(episodes={1: [6]}, schedule_future=True, release_time="22:00")
    job = jobs._jobs[pai]
    assert job["status"] == "scheduled"
    assert job["next_search_at"] == f"{HOJE.isoformat()}T22:00:00"
    assert indexador == {}


def test_serie_buscar_agora_procura_o_que_nao_estreou(serie_tmdb, indexador, qbit, parar_no_download):
    pai = _criar_serie(episodes={1: [4]}, schedule_future=True)
    indexador["Serie Exemplo S01"] = [_hit("Serie Exemplo S01E04 1080p WEB-DL")]
    indexador["Serie Teste S01"] = [_hit("Serie Teste S01E04 1080p WEB-DL Dublado")]

    async def go():
        schedule.search_now(pai)
        await _drain()
    asyncio.run(go())
    assert parar_no_download == ["downloading"]


def test_serie_sem_resultado_agenda_com_a_escolha_aberta(serie_tmdb, indexador, qbit, parar_no_download):
    pai = _criar_serie(episodes={1: [1]})
    job = jobs._jobs[pai]
    assert job["status"] == "scheduled"
    assert job["awaiting"]["reason"] == "manual_pick"
    assert job["detail"].startswith("Nenhum torrent encontrado para o original e o dublado")
    # resolver a escolha no programado segue o fluxo normal
    orig = "magnet:?xt=urn:btih:" + "1" * 40 + "&dn=Serie.Exemplo.S01E01.1080p.WEB-DL"
    dub = "magnet:?xt=urn:btih:" + "2" * 40 + "&dn=Serie.Teste.S01E01.720p.Dublado"

    async def go():
        await pipeline.resolve(pai, "manual_pick", {"replace_plan": True, "torrents": [
            {"magnet": orig, "role": "original", "episodes": "auto"},
            {"magnet": dub, "role": "dubbed", "episodes": "auto"}]})
        await _drain()
    asyncio.run(go())
    assert parar_no_download == ["downloading"]


def test_serie_busca_incremental_e_qualidade_minima(serie_tmdb, indexador, qbit, relogio,
                                                    parar_no_download):
    indexador["Serie Exemplo S01"] = [_hit("Serie Exemplo S01E01 720p WEB-DL", "t2")]
    indexador["Serie Teste S01"] = [_hit("Serie Teste S01E01 1080p WEB-DL Dublado", "t1")]
    pai = _criar_serie(episodes={1: [1]},
                       min_quality={"video": {"res": "1080p", "source": None}})
    job = jobs._jobs[pai]
    assert job["status"] == "scheduled"
    assert "qualidade mínima para o original" in job["detail"]
    # t2 falhou na nova tentativa (nada) e o dublado sumiu de t1 (respondeu
    # com outro): o 720p fica na lista, o dublado antigo sai, o novo entra
    indexador["Serie Exemplo S01"] = []
    indexador["Serie Teste S01"] = [_hit("Serie Teste S01E01 2160p WEB-DL Dublado", "t1")]
    relogio["t"] += timedelta(hours=7)

    async def go():
        assert schedule.tick() == 1
        await _drain()
    asyncio.run(go())
    titulos = {c["title"] for c in job["search_candidates"] if c["rank"] is not None}
    assert titulos == {"Serie Exemplo S01E01 720p WEB-DL",
                       "Serie Teste S01E01 2160p WEB-DL Dublado"}
    assert job["status"] == "scheduled"     # o original segue abaixo do piso


def test_episodio_futuro_programado_aparece_como_pedido(serie_tmdb, indexador):
    from services.series import activity
    _criar_serie(episodes={1: [4]}, schedule_future=True)
    eps = activity.episodes_in_jobs(4242)["seasons"][1]
    assert [(e["episode"], e["job_status"]) for e in eps] == [(4, "scheduled")]


# ============================== falha passageira ==============================

def test_jackett_fora_do_ar_tenta_de_novo(filme_tmdb, relogio, monkeypatch):
    import httpx

    async def fora(query, indexer="all", categories=None):
        raise httpx.ConnectError("connection refused")
    monkeypatch.setattr(jackett, "search", fora)
    job = _criar_filme()
    assert job["status"] == "scheduled"
    assert job["detail"].startswith("Busca falhou (ConnectError) — nova tentativa 10/11 às 18:00")
    # com a busca contínua desligada, segue falhando como antes
    job2 = _criar_filme(retry_mode="off")
    assert job2["status"] == "error"


def test_serie_tmdb_fora_do_ar_tenta_de_novo(serie_tmdb, relogio, monkeypatch):
    import httpx

    async def fora(tv_id, lang):
        raise httpx.ReadTimeout("")
    monkeypatch.setattr(tmdb, "tv_details", fora)
    job = jobs._jobs[_criar_serie(episodes={1: [1]})]
    assert job["status"] == "scheduled"
    assert job["detail"].startswith("Busca falhou (ReadTimeout)")


def test_programacao_sobrevive_ao_banco(filme_tmdb, relogio):
    """Os campos da programação voltam iguais do banco (restart)."""
    from services import store
    job = _criar_filme(scheduled_at="2026-11-20T08:00", retry_mode="on",
                       min_quality={"audio": {"res": "1080p", "source": "webdl"}})
    jobs.runtime.flush_dirty(force=True)
    store.upsert_job(job)
    lido = store.get_job(job["id"])
    for k in ("status", "scheduled_at", "next_search_at", "retry_mode", "min_quality"):
        assert lido[k] == job[k], k
    assert lido["status"] == "scheduled"
    # e o load() do startup traz o programado para a memória (o relógio o vê)
    jobs._jobs.clear()
    jobs.load()
    assert jobs._jobs[job["id"]]["next_search_at"] == "2026-11-20T08:00:00"
