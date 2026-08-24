"""Modo "pular busca de torrents": o indexador não é consultado e o magnet
próprio entra no GATE de escolha (não só na troca durante o download)."""
import asyncio

import pytest

from services import jobs
from services.jobs import actions, movies
from services.series import pipeline


# -------------------- validação na criação --------------------

def test_pular_busca_exige_modo_manual(temp_db):
    async def go():
        with pytest.raises(ValueError, match="modo manual"):
            await movies.create(1, "pt", mode="auto", skip_search=True)
        with pytest.raises(ValueError, match="modo manual"):
            await pipeline.create_series(1, "pt", seasons=[1], mode="auto",
                                         skip_search=True)
    asyncio.run(go())


# -------------------- filme: o gate abre sem consultar o indexador --------------------

def _job_awaiting(**extra):
    job = {
        "id": "sk1", "tmdb_id": 7, "language": "pt", "mode": "manual",
        "kind": "both", "status": "awaiting", "detail": "", "output": None,
        "movie": {"original_title": "Filme Exemplo", "year": "2020"},
        "created_at": "2026-01-01T00:00:00",
        "progress": {"video": None, "audio": None},
        "search": {"video": [], "audio": []}, "fallbacks": {},
        "video_torrent": None, "audio_torrent": None, "current": {},
    }
    job.update(extra)
    return job


def test_run_com_skip_search_nao_chama_o_indexador(temp_db, monkeypatch):
    job = _job_awaiting(status="searching", skip_search=True)
    jobs._jobs["sk1"] = job
    chamou = []
    monkeypatch.setattr(movies.search, "_search",
                        lambda j: chamou.append("busca"))

    async def carrega(j):
        j["movie"] = {"original_title": "Filme Exemplo", "year": "2020",
                      "localized_title": "Filme Exemplo"}
        return j["movie"]
    monkeypatch.setattr(movies.search, "_load_movie", carrega)
    # o gate é o fim da linha: nada de download
    monkeypatch.setattr(movies.downloads, "_start_download",
                        lambda *a, **k: chamou.append("download"))

    asyncio.run(movies._run(job))
    assert chamou == []                       # nem busca nem download
    assert job["status"] == "awaiting"
    assert job["search"] == {"audio": [], "video": []}
    assert job["movie"]["year"] == "2020"     # metadados do TMDB carregados


def test_select_aceita_magnet_proprio_nos_dois_papeis(temp_db, monkeypatch):
    job = _job_awaiting(skip_search=True)
    jobs._jobs["sk1"] = job
    baixados = {}

    async def fake_download(j, a, v):
        baixados["audio"], baixados["video"] = a, v
    monkeypatch.setattr(movies, "_download_and_merge", fake_download)

    async def go():
        out = await actions.select("sk1", None, None, custom={
            "audio": {"url": "magnet:?xt=urn:btih:aaa", "title": "Dublado 1080p"},
            "video": {"url": "https://x/original.torrent", "title": ""},
        })
        assert out is not None
    asyncio.run(go())

    assert baixados["audio"]["title"] == "Dublado 1080p"
    assert baixados["audio"]["magnet"] == "magnet:?xt=urn:btih:aaa"
    assert baixados["video"]["link"] == "https://x/original.torrent"
    assert baixados["video"]["title"] == "original.torrent"   # título vem do link
    # entram na busca do job: aparecem na lista e servem de reserva depois
    assert job["search"]["audio"][0]["tracker"] == "manual"
    assert job["search"]["video"][0]["tracker"] == "manual"
    assert not job.get("awaiting")     # o gate se resolveu


def test_select_mistura_candidato_da_busca_com_magnet_proprio(temp_db, monkeypatch):
    """Busca normal (não pulada): dá para pegar o áudio da lista e informar o
    vídeo à mão."""
    cand = {"id": "a1", "title": "Dublado da busca", "seeders": 5, "size": 1,
            "score": 1, "edition": None, "magnet": "magnet:?xt=urn:btih:busca"}
    job = _job_awaiting(search={"audio": [cand], "video": []})
    jobs._jobs["sk1"] = job
    baixados = {}

    async def fake_download(j, a, v):
        baixados["audio"], baixados["video"] = a, v
    monkeypatch.setattr(movies, "_download_and_merge", fake_download)

    asyncio.run(actions.select("sk1", "a1", None, custom={
        "video": {"url": "magnet:?xt=urn:btih:meu", "title": "Original 2160p"}}))
    assert baixados["audio"]["id"] == "a1"
    assert baixados["video"]["title"] == "Original 2160p"


def test_select_sem_candidato_nem_magnet_reclama(temp_db):
    job = _job_awaiting()
    jobs._jobs["sk1"] = job

    async def go():
        with pytest.raises(ValueError, match="[Áá]udio"):
            await actions.select("sk1", "nao-existe", None)
    asyncio.run(go())
    assert job["status"] == "awaiting"      # o gate continua de pé


def test_select_recusa_url_invalida_e_mantem_o_gate(temp_db):
    job = _job_awaiting(skip_search=True)
    jobs._jobs["sk1"] = job

    async def go():
        with pytest.raises(ValueError, match="magnet"):
            await actions.select("sk1", None, None,
                                 custom={"audio": {"url": "ftp://x/y.torrent"}})
    asyncio.run(go())
    assert job["search"]["audio"] == []
    assert job["status"] == "awaiting"


# -------------------- série: mesma regra na fase 1 e na fase 2 --------------------

def test_serie_com_skip_search_gateia_manual_sem_buscar(temp_db, monkeypatch):
    chamou = []
    monkeypatch.setattr(pipeline, "_search_series",
                        lambda j: chamou.append("fase1"))
    monkeypatch.setattr(pipeline, "_phase2_search",
                        lambda j, m: chamou.append("fase2"))
    job = {
        "id": "sr1", "media_type": "tv", "tmdb_id": 9, "language": "pt",
        "mode": "manual", "kind": "series", "skip_search": True,
        "status": "searching", "detail": "", "output": None, "progress": {},
        "movie": {"original_title": "Serie Exemplo", "localized_title": "Serie Exemplo",
                  "english_title": None, "year": "2020"},
        "created_at": "2026-01-01T00:00:00",
        "known_seasons": [1],
        "episodes": {"S01E01": {"season": 1, "episode": 1, "name": "Um",
                                "air_date": "2020-01-01", "runtime": 40,
                                "state": "pending", "src": {}, "output": None,
                                "error": None}},
        "torrents": [], "awaiting": None, "report": None, "order_map": None,
        "search_tv": {"original": {}, "dubbed": {}},
    }
    jobs._jobs["sr1"] = job

    asyncio.run(pipeline._plan_and_gate(job))
    assert chamou == []                    # nenhuma das duas fases rodou
    assert job["status"] == "awaiting"
    assert job["awaiting"]["reason"] == "manual_pick"
    # a lista vem vazia: é no gate que o usuário cola os magnets
    assert job["awaiting"]["payload"]["by_torrent"] == {"original": [], "dubbed": []}


# -------------------- duplicata e cancel de duplicado --------------------

def test_create_manual_recusa_par_duplicado_ativo(temp_db, tmp_path, monkeypatch):
    """Duas automações criando o job do MESMO par (caso real): o segundo tem
    que ser recusado — os dois registrariam a mesma saída e cancelar um
    apagava o filme entregue pelo outro."""
    from services.jobs import movies as movies_mod
    vf, af = tmp_path / "v.mkv", tmp_path / "a.mkv"
    vf.write_bytes(b"x")
    af.write_bytes(b"x")
    temp_db.add_destination("Filmes", str(tmp_path / "out"), True)
    monkeypatch.setattr(movies_mod, "_probe_manual_file", lambda p, r: None)

    async def fake_details(tmdb_id, lang):
        return {"original_title": "Filme Exemplo", "localized_title": "Filme Exemplo",
                "year": "2014", "poster": None}
    monkeypatch.setattr(movies_mod.tmdb, "details", fake_details)

    async def parado(job, video_file, audio_file):
        await asyncio.sleep(30)
    monkeypatch.setattr(movies_mod, "_run_manual", parado)

    async def go():
        j1 = await movies_mod.create_manual(9, "pt", str(vf), str(af))
        with pytest.raises(ValueError, match="Já existe um job ativo"):
            await movies_mod.create_manual(9, "pt", str(vf), str(af))
        # cancelado/terminado libera a recriação
        jobs._jobs[j1["id"]]["status"] = "cancelled"
        j2 = await movies_mod.create_manual(9, "pt", str(vf), str(af))
        assert j2["id"] != j1["id"]
        for jid in (j1["id"], j2["id"]):
            t = jobs._tasks.pop(jid, None)
            if t: t.cancel()
    asyncio.run(go())


def test_cancel_nao_apaga_saida_de_outro_job(temp_db, tmp_path):
    """O arquivo no caminho de saída é mais antigo que o merge deste job =
    foi outro job que o entregou; cancelar este NÃO pode apagá-lo."""
    import os, time
    from services.jobs import runtime
    out = tmp_path / "Filme (2014)" / "Filme (2014) [pt+orig].mkv"
    out.parent.mkdir(parents=True)
    out.write_bytes(b"filme pronto de ontem")
    antigo = time.time() - 3600
    os.utime(out, (antigo, antigo))
    job = {"id": "c1", "tmdb_id": 1, "language": "pt", "status": "cancelled",
           "detail": "", "output": str(out), "events": [],
           "created_at": "2026-01-01T00:00:00",
           "merge_started_at": __import__("datetime").datetime.now()
               .isoformat(timespec="seconds")}
    runtime._delete_output(job)
    assert out.is_file(), "arquivo de outro job foi apagado"

    # já um arquivo escrito DEPOIS do início do merge é parcial deste job: sai
    out.write_bytes(b"parcial deste job")
    runtime._delete_output(job)
    assert not out.exists()
