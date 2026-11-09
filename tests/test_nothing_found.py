"""Busca sem torrent no Jackett NÃO encerra o job.

Antes, um papel sem nenhum candidato (ou os dois) fazia o filme falhar
("Nenhum torrent encontrado com áudio em ...") e a série terminar em "Plano
vazio" depois de um aviso de lacunas sem saída. Agora o job fica AGUARDANDO
na escolha — a mesma do modo manual, que aceita magnet/link próprio por
papel — e dá para buscar de novo no mesmo job. Daí, colar um magnet segue o
fluxo normal até o download.

Jackett e TMDB são funções falsas; o qBittorrent é um duplo que registra o
que foi enviado.
"""
import asyncio

import pytest

from services import candidates, jackett, jobs, tmdb
from services.jobs import actions, movies
from services.series import pipeline


def _hit(title, seeders=10, size=2000):
    return {"title": title, "seeders": seeders, "size": size,
            "magnet": f"magnet:?xt=urn:btih:{abs(hash(title)) % 10 ** 12:040x}",
            "link": None, "tracker": "t1", "tracker_id": "t1",
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
    """Jackett falso: devolve o que estiver em `por_query` (vazio = nada)."""
    por_query: dict[str, list[dict]] = {}

    async def fake_search(query, indexer="all", categories=None):
        return list(por_query.get(query, []))
    monkeypatch.setattr(jackett, "search", fake_search)
    return por_query


@pytest.fixture
def qbit(monkeypatch):
    q = FakeQbit()
    monkeypatch.setattr(jobs.runtime, "_qbit", q)   # filmes
    monkeypatch.setattr(jobs, "_qbit", q)           # séries (via fachada)
    return q


async def _run_and_wait(job_id, coro):
    out = await coro
    task = jobs._tasks.get(job_id)
    if task:
        await task
    return out


# ============================== filme ==============================

def _movie_job(mode="auto", kind="both"):
    return {"id": "nf1", "tmdb_id": 7, "language": "pt", "mode": mode,
            "kind": kind, "status": "searching", "detail": "", "output": None,
            "movie": None, "created_at": "2026-11-08T00:00:00",
            "progress": {"video": None, "audio": None},
            "search_queries": [], "search_candidates": []}


@pytest.fixture
def filme(temp_db, monkeypatch):
    async def fake_details(tmdb_id, lang):
        return {"id": tmdb_id, "original_title": "Filme Exemplo",
                "localized_title": "Filme Teste", "english_title": None,
                "original_language": "en", "year": "2014", "overview": None,
                "poster": None}
    monkeypatch.setattr(tmdb, "details", fake_details)
    job = _movie_job()
    jobs._jobs[job["id"]] = job
    yield job
    jobs._jobs.pop(job["id"], None)


def _parar_depois_do_envio(monkeypatch):
    chegou = []

    async def watchdog(job):
        chegou.append(job["status"])
    monkeypatch.setattr(movies, "_run_from_download", watchdog)
    return chegou


def test_filme_sem_dublado_fica_aguardando_e_magnet_segue_ate_o_download(
        filme, indexador, qbit, monkeypatch):
    # o original existe no indexador; o dublado, não
    indexador["Filme Exemplo 2014"] = [_hit("Filme Exemplo 2014 1080p WEB-DL")]
    asyncio.run(movies._run(filme))

    assert filme["status"] == "awaiting"          # não "error"
    assert "Nenhum torrent encontrado para o dublado" in filme["detail"]
    assert "magnet" in filme["detail"] and "buscar de novo" in filme["detail"]
    detalhe = jobs.get_job(filme["id"])
    # a escolha abre: a lista do original e a do dublado VAZIA (é ali que o
    # magnet é colado)
    assert detalhe["search"]["audio"] == []
    assert [c["title"] for c in detalhe["search"]["video"]] == [
        "Filme Exemplo 2014 1080p WEB-DL"]
    assert qbit.added == []

    chegou = _parar_depois_do_envio(monkeypatch)
    video_id = detalhe["search"]["video"][0]["id"]
    magnet = "magnet:?xt=urn:btih:" + "f" * 40
    asyncio.run(_run_and_wait(filme["id"], actions.select(
        filme["id"], None, video_id,
        custom={"audio": {"url": magnet, "title": "Filme Teste 2014 1080p Dublado"}})))

    assert chegou == ["downloading"]
    enviados = dict((tag, url) for url, tag in qbit.added)
    assert enviados[f"dl-{filme['id']}-audio"] == magnet
    assert f"dl-{filme['id']}-video" in enviados


def test_filme_sem_nada_nos_dois_papeis_tambem_aguarda(filme, indexador):
    asyncio.run(movies._run(filme))
    assert filme["status"] == "awaiting"
    assert filme["detail"].startswith(
        "Nenhum torrent encontrado para o dublado e o original")
    assert jobs.get_job(filme["id"])["search"] == {"audio": [], "video": []}


def test_filme_buscar_de_novo_no_mesmo_job(filme, indexador, qbit, monkeypatch):
    asyncio.run(movies._run(filme))
    assert filme["status"] == "awaiting"
    # o release apareceu no indexador: a nova busca segue o modo auto
    indexador["Filme Exemplo 2014"] = [_hit("Filme Exemplo 2014 1080p WEB-DL")]
    indexador["Filme Teste 2014"] = [_hit("Filme Teste 2014 1080p Dublado")]
    chegou = _parar_depois_do_envio(monkeypatch)
    out = asyncio.run(_run_and_wait(filme["id"], actions.research(filme["id"])))
    assert out["id"] == filme["id"]               # o mesmo job, não um novo
    assert chegou == ["downloading"]
    assert len(qbit.added) == 2


def test_filme_buscar_de_novo_recusado_com_torrent_no_ar(filme):
    filme["status"] = "downloading"
    with pytest.raises(ValueError, match="aguarda a escolha"):
        asyncio.run(actions.research(filme["id"]))


def test_filme_sem_corte_compativel_vai_para_a_escolha(filme, indexador):
    """Há dublado e original, mas de cortes diferentes: em vez de falhar, o
    usuário escolhe o par à mão (a UI avisa do corte) ou cola um magnet."""
    indexador["Filme Exemplo 2014"] = [_hit("Filme Exemplo 2014 1080p WEB-DL")]
    indexador["Filme Teste 2014"] = [
        _hit("Filme Teste 2014 Extended 1080p Dublado")]
    asyncio.run(movies._run(filme))
    assert filme["status"] == "awaiting"
    assert "mesmo corte" in filme["detail"]
    busca = jobs.get_job(filme["id"])["search"]
    assert busca["audio"] and busca["video"]


# ============================== série ==============================

def _ep(e):
    return {"season": 1, "episode": e, "name": f"Ep {e}", "air_date": "2008-01-20",
            "runtime": 48, "state": "pending", "src": {}, "output": None,
            "error": None}


@pytest.fixture
def serie(temp_db):
    job = {
        "id": "nf2", "media_type": "tv", "tmdb_id": 4242, "language": "pt",
        "mode": "auto", "kind": "both", "status": "searching", "detail": "",
        "movie": {"original_title": "Serie Exemplo", "localized_title": "Serie Teste",
                  "english_title": None, "year": "2008", "poster": None,
                  "overview": None, "original_language": "en", "id": 4242},
        "request": {"seasons": [1], "episodes": {}}, "known_seasons": [1],
        "episodes": {"S01E01": _ep(1), "S01E02": _ep(2)},
        "torrents": [], "awaiting": None, "order_map": None, "report": None,
        "progress": {}, "output": None, "torrent_save_path": "",
        "search_queries": [], "search_candidates": [],
        "created_at": "2026-11-08T00:00:00",
    }
    jobs._jobs[job["id"]] = job
    yield job
    jobs._jobs.pop(job["id"], None)
    pipeline._pools.pop(job["id"], None)


def test_serie_busca_vazia_cai_na_escolha_manual_e_magnet_baixa(
        serie, indexador, qbit, monkeypatch):
    asyncio.run(pipeline._plan_and_gate(serie))

    assert serie["status"] == "awaiting"          # nem erro, nem "Plano vazio"
    assert serie["awaiting"]["reason"] == "manual_pick"
    # o job continua auto: a escolha abriu porque não havia o que escolher
    assert serie["mode"] == "auto"
    assert serie["detail"].startswith(
        "Nenhum torrent encontrado para o original e o dublado")
    payload = jobs._public(serie)["awaiting"]["payload"]
    assert payload["empty_roles"] == ["original", "dubbed"]
    assert payload["can_research"] is True
    assert payload["preselected"] == []

    chegou = []

    async def watchdog(job):
        chegou.append(job["status"])
    monkeypatch.setattr(pipeline, "_wait_downloads", watchdog)
    orig = ("magnet:?xt=urn:btih:" + "1" * 40
            + "&dn=Serie.Exemplo.S01.1080p.WEB-DL")
    dub = ("magnet:?xt=urn:btih:" + "2" * 40
           + "&dn=Serie.Teste.S01.720p.Dublado")
    asyncio.run(_run_and_wait(serie["id"], pipeline.resolve(
        serie["id"], "manual_pick", {"replace_plan": True, "torrents": [
            {"magnet": orig, "role": "original", "episodes": "auto"},
            {"magnet": dub, "role": "dubbed", "episodes": "auto"}]})))

    assert chegou == ["downloading"]
    assert [url for url, _ in qbit.added] == [orig, dub]
    assert {t["role"]: t["coverage"] for t in serie["torrents"]} == {
        "original": ["S01E01", "S01E02"], "dubbed": ["S01E01", "S01E02"]}


def test_serie_sem_dublado_mantem_o_original_pre_selecionado(serie, indexador):
    indexador["Serie Exemplo S01"] = [_hit("Serie Exemplo S01 1080p WEB-DL", size=9000)]
    asyncio.run(pipeline._plan_and_gate(serie))
    assert serie["awaiting"]["reason"] == "manual_pick"
    assert "para o dublado" in serie["detail"]
    payload = jobs._public(serie)["awaiting"]["payload"]
    assert payload["empty_roles"] == ["dubbed"]
    assert [(p["role"], p["coverage"]) for p in payload["preselected"]] == [
        ("original", ["S01E01", "S01E02"])]


def test_serie_buscar_de_novo_refaz_o_plano(serie, indexador, qbit, monkeypatch):
    asyncio.run(pipeline._plan_and_gate(serie))
    assert serie["torrents"] == []
    indexador["Serie Exemplo S01"] = [_hit("Serie Exemplo S01 1080p WEB-DL", size=9000)]
    indexador["Serie Teste S01"] = [_hit("Serie Teste S01 720p WEB-DL Dublado", size=7000)]
    chegou = []

    async def watchdog(job):
        chegou.append(job["status"])
    monkeypatch.setattr(pipeline, "_wait_downloads", watchdog)
    out = asyncio.run(_run_and_wait(serie["id"], pipeline.research(serie["id"])))
    assert out["id"] == serie["id"]               # o mesmo job, não um novo
    # job auto que achou os dois papéis na nova busca segue sozinho
    assert chegou == ["downloading"]
    assert sorted(t["role"] for t in serie["torrents"]) == ["dubbed", "original"]
    assert len(qbit.added) == 2


def test_serie_manual_buscar_de_novo_reabre_a_escolha(serie, indexador):
    serie["mode"] = "manual"
    asyncio.run(pipeline._plan_and_gate(serie))
    indexador["Serie Exemplo S01"] = [_hit("Serie Exemplo S01 1080p WEB-DL", size=9000)]
    asyncio.run(_run_and_wait(serie["id"], pipeline.research(serie["id"])))
    # ainda sem dublado: a escolha reabre, agora com o original pré-selecionado
    assert serie["awaiting"]["reason"] == "manual_pick"
    payload = jobs._public(serie)["awaiting"]["payload"]
    assert payload["empty_roles"] == ["dubbed"]
    assert [p["role"] for p in payload["preselected"]] == ["original"]


def test_lacunas_sem_nada_coberto_nao_deixam_continuar(serie):
    """O aviso de lacunas com TODOS os episódios faltando num papel: aceitar
    só levaria ao "Plano vazio" — fica recusado e o gate continua."""
    serie["status"] = "awaiting"
    serie["awaiting"] = {"reason": "gaps_confirm", "payload": None}
    serie["torrents"] = [{
        "n": 0, "role": "original", "tag": "dl-nf2-t0", "title": "Serie Exemplo S01",
        "tracker": "t", "seeders": 1, "size": 1, "quality": None,
        "coverage_label": None, "magnet": "magnet:?xt=urn:btih:" + "9" * 40,
        "link": None, "infohash": None, "files_count": None,
        "coverage": ["S01E01", "S01E02"], "state": "pending", "hash": None,
        "progress": None, "selected_files": None, "content_path": None}]
    assert jobs._public(serie)["awaiting"]["payload"]["nothing_left"] is True
    with pytest.raises(ValueError, match="não há o que baixar"):
        asyncio.run(pipeline.resolve(serie["id"], "gaps_confirm", {"accept": True}))
    assert serie["awaiting"]["reason"] == "gaps_confirm"
    assert serie["status"] == "awaiting"
