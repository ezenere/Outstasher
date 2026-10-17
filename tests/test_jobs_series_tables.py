"""Episódios e torrents dos jobs de série em tabelas (job_episodes /
episode_subs / job_torrents / torrent_coverage / torrent_files) em vez de
dentro do documento do job.

Cobre: decomposição -> recomposição (round-trip do dict, incluindo order_map,
legendas, EDL, chaves desconhecidas e o torrent que aponta para o candidato),
gravação só da LINHA que mudou (progresso de um torrent não regrava os
episódios), candidato reescrito/sumido com torrent apontando para ele, job
apagado levando as linhas filhas, leitura do formato antigo, o relatório
derivado dos episódios e o id estável de magnet/link próprio.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

from services import candidates, jobs, store
from services.jobs import search, views
from services.series import merge_runner, pipeline

ROOT = Path(__file__).resolve().parent.parent

PACK = {"title": "Serie Exemplo S01 1080p", "tracker": "t", "seeders": 3,
        "size": 900, "quality": "1080p WEB-DL", "coverage": "S01 completa",
        "score": 28.0, "tier": 28, "magnet": "magnet:?xt=urn:btih:" + "a" * 40,
        "link": None, "infohash": None, "files": 12}


def _ep(s, e, state="pending", **kw):
    ep = {"season": s, "episode": e, "name": f"Episódio {e}",
          "air_date": "2020-01-01", "runtime": 40, "state": state,
          "src": {}, "output": None, "error": None}
    ep.update(kw)
    return ep


def _torrent(n, role, coverage, **kw):
    t = {"n": n, "role": role, "tag": f"dl-tvx-t{n}", "title": f"Serie Exemplo t{n}",
         "tracker": "manual", "seeders": None, "size": 0, "quality": None,
         "coverage_label": None, "magnet": f"magnet:?xt=urn:btih:{n}" * 2,
         "link": None, "infohash": None, "files_count": None,
         "coverage": list(coverage), "state": "pending", "hash": None,
         "progress": None, "selected_files": None, "content_path": None}
    t.update(kw)
    return t


def _from_cand(n, cand, coverage, **kw):
    """Torrent do plano como o pipeline o monta: cópia do candidato."""
    return _torrent(n, cand["role"], coverage, title=cand["title"],
                    tracker=cand["tracker"], seeders=cand["seeders"],
                    size=cand["size"], quality=cand["quality"],
                    coverage_label=cand["coverage"], magnet=cand["magnet"],
                    link=cand["link"], infohash=cand["infohash"],
                    files_count=cand["files"], **kw)


def _serie(jid="tvx", n_eps=4) -> dict:
    job = {"id": jid, "media_type": "tv", "tmdb_id": 9, "language": "pt",
           "mode": "auto", "kind": "both", "status": "downloading",
           "detail": "", "output": None, "progress": {},
           "created_at": "2026-01-01T00:00:00", "movie": None,
           "request": {"seasons": [1], "episodes": {}},
           "known_seasons": [1, 2], "awaiting": None, "order_map": None,
           "episodes": {f"S01E{e:02d}": _ep(1, e) for e in range(1, n_eps + 1)},
           "torrents": [], "search_queries": [], "search_candidates": []}
    candidates.set_series(job, "original", {k: [PACK] for k in job["episodes"]})
    return job


def _data(jid) -> dict:
    with store._lock:
        return json.loads(store._conn.execute(
            "SELECT data FROM jobs WHERE id = ?", (jid,)).fetchone()[0])


def _count(table, jid) -> int:
    with store._lock:
        return store._conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE job_id = ?", (jid,)).fetchone()[0]


class _Sql:
    """Captura os comandos que o SQLite executa durante o bloco."""

    def __enter__(self):
        self.stmts: list[str] = []
        store._conn.set_trace_callback(self.stmts.append)
        return self

    def __exit__(self, *exc):
        store._conn.set_trace_callback(None)

    def writes(self, table) -> list[str]:
        return [s for s in self.stmts
                if s.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
                and f" {table} " in f" {s} ".replace("(", " ")]


# -------------------- round-trip --------------------

def _rica() -> dict:
    job = _serie()
    pack = candidates.of_role(job, "original")[0]
    eps = job["episodes"]
    eps["S01E01"].update(state="done", output="/dest/S01E01.mkv",
                         src={"original": "/dl/o/S01E01.mkv", "dubbed": "/dl/d/1.mkv"},
                         subs={"original": ["/dl/o/S01E01.en.srt", "/dl/o/S01E01.es.srt"],
                               "dubbed": []},
                         edl={"version": 3, "segments": [{"kind": "match", "a0": 0.0,
                                                          "a1": 10.5, "offset": -1.25}],
                              "confidence_profile": [0.9, 0.85, 1.0]})
    eps["S01E02"].update(state="failed", error="arquivo não baixado/encontrado: Dublado",
                         src={"original": "/dl/o/S01E02.mkv"}, pista_nova={"x": 1})
    job["order_map"] = {"S01E01": [1, 2], "S01E02": [1, 1]}
    job["torrents"] = [
        _from_cand(0, pack, ["S01E01", "S01E02", "S01E03"], state="downloading",
                   hash="h0", coverage_shrunk=["S01E04"],
                   selected_files=["Pack/S01E01.mkv", "Pack/S01E02.mkv"],
                   progress={"pct": 42.5, "speed": 1000, "eta": 60,
                             "state": "downloading", "seeds": 3, "name": "Pack",
                             "size": 9000, "downloaded": 3825}),
        # magnet próprio do usuário: sem candidato
        _torrent(1, "dubbed", ["S01E04"], state="done", hash="h1",
                 content_path="/dl/d", selected_files=[]),
    ]
    return job


def test_round_trip_episodios_e_torrents(temp_db):
    job = _rica()
    store.upsert_job(job)
    back = store.get_job(job["id"])
    exp_eps = json.loads(json.dumps(job["episodes"]))
    del exp_eps["S01E01"]["subs"]["dubbed"]    # lista vazia = sem legenda
    assert back["episodes"] == exp_eps
    assert list(back["episodes"]) == list(job["episodes"])   # a ordem também
    assert back["order_map"] == job["order_map"]
    assert back["torrents"] == job["torrents"]

    # o documento não carrega mais nada disso (nem o relatório)
    data = _data(job["id"])
    assert not any(k in data for k in ("episodes", "torrents", "order_map", "report"))
    # temporadas conhecidas: metadado da série (media_seasons), derivado
    assert "known_seasons" not in data and back["known_seasons"] == [1, 2]

    # o torrent do plano aponta para o candidato e não repete os campos; o
    # magnet próprio grava os seus
    with store._lock:
        rows = store._conn.execute(
            "SELECT n, cid, title, magnet FROM job_torrents WHERE job_id = ? "
            "ORDER BY n", (job["id"],)).fetchall()
    assert rows[0][1:] == ("o0", None, None)
    assert rows[1][1] is None and rows[1][2] == "Serie Exemplo t1"

    # sem mudança nenhuma, gravar de novo é idempotente
    store.upsert_job(back, create=False)
    assert store.get_job(job["id"])["torrents"] == job["torrents"]


def test_serie_sem_ordem_alternativa_e_recompressao(temp_db):
    job = _serie(n_eps=1)
    job.pop("order_map")
    job["mode"] = "recompress"
    job["episodes"] = {"S01E01": _ep(1, 1, "downloaded", src={"file": "/lib/a.mkv"},
                                     rel="Season 01/a.mkv")}
    store.upsert_job(job)
    back = store.get_job(job["id"])
    assert back["episodes"] == job["episodes"]
    assert back["order_map"] is None and back["torrents"] == []


def test_filme_nao_ganha_secoes_de_serie(temp_db):
    filme = {"id": "mv9", "status": "done", "created_at": "2026-01-01T00:00:00",
             "language": "pt", "progress": {}, "search_queries": [],
             "search_candidates": []}
    store.upsert_job(filme)
    back = store.get_job("mv9")
    assert not any(k in back for k in ("episodes", "torrents", "order_map"))
    assert "report" not in jobs._public(back)


# -------------------- só a linha que mudou --------------------

def test_progresso_de_um_torrent_nao_regrava_episodios(temp_db):
    job = _rica()
    job["episodes"].update({f"S02E{e:02d}": _ep(2, e) for e in range(1, 200)})
    store.upsert_job(job)

    job["torrents"][0]["progress"] = dict(job["torrents"][0]["progress"], pct=43.0)
    job["detail"] = "Baixando torrents (1/2 concluídos)..."
    with _Sql() as sql:
        store.upsert_job(job, create=False)
    assert sql.writes("job_episodes") == []
    assert sql.writes("episode_subs") == []
    assert sql.writes("torrent_coverage") == [] and sql.writes("torrent_files") == []
    escritos = sql.writes("job_torrents")
    assert len(escritos) == 1 and "'dl-tvx-t0'" in escritos[0]
    assert store.get_job(job["id"])["torrents"][0]["progress"]["pct"] == 43.0

    # um episódio concluído regrava só a linha dele
    job["episodes"]["S01E03"].update(state="done", output="/dest/S01E03.mkv")
    with _Sql() as sql:
        store.upsert_job(job, create=False)
    assert len(sql.writes("job_episodes")) == 1
    assert "'S01E03'" in sql.writes("job_episodes")[0]
    assert sql.writes("job_torrents") == []

    # cobertura encolheu: os filhos do torrent mudam, a linha não
    job["torrents"][0]["coverage"].remove("S01E03")
    with _Sql() as sql:
        store.upsert_job(job, create=False)
    assert sql.writes("job_torrents") == []
    assert sql.writes("torrent_coverage")
    back = store.get_job(job["id"])
    assert back["torrents"][0]["coverage"] == ["S01E01", "S01E02"]
    assert back["episodes"]["S01E03"]["state"] == "done"

    # episódio/torrent removido do dict sai do banco
    del job["episodes"]["S02E199"]
    job["torrents"].pop()
    store.upsert_job(job, create=False)
    back = store.get_job(job["id"])
    assert "S02E199" not in back["episodes"] and len(back["torrents"]) == 1
    assert _count("torrent_coverage", job["id"]) == 3   # 2 + o encolhido


def test_restart_reescreve_a_secao_inteira_uma_vez(temp_db):
    """Processo novo não tem o hash das linhas: o primeiro upsert reescreve a
    seção inteira (e apaga o que não existe mais), os seguintes só o que mudou."""
    job = _rica()
    store.upsert_job(job)
    store._section_hash.clear()                  # "restart"
    job = store.get_job(job["id"])
    del job["episodes"]["S01E04"]
    store.upsert_job(job, create=False)
    assert _count("job_episodes", job["id"]) == 3
    job["torrents"][1]["state"] = "abandoned"
    with _Sql() as sql:
        store.upsert_job(job, create=False)
    assert len(sql.writes("job_torrents")) == 1
    assert sql.writes("job_episodes") == []


# -------------------- referência ao candidato --------------------

def test_candidatos_reescritos_com_torrent_apontando(temp_db):
    job = _rica()
    store.upsert_job(job)
    # a seção de candidatos é reescrita (DELETE + INSERT): a FK adiada aceita
    candidates.add_query(job, "original", "Serie Exemplo S01", 1)
    store.upsert_job(job, create=False)
    assert store.get_job(job["id"])["torrents"] == job["torrents"]

    # candidato some (busca refeita) com o torrent ainda no plano: o torrent
    # passa a gravar os próprios campos, sem perder nada
    candidates.reset(job)
    store.upsert_job(job, create=False)
    back = store.get_job(job["id"])
    assert back["torrents"] == job["torrents"]
    with store._lock:
        cid, title = store._conn.execute(
            "SELECT cid, title FROM job_torrents WHERE job_id = ? AND n = 0",
            (job["id"],)).fetchone()
    assert cid is None and title == PACK["title"]


def test_job_apagado_leva_as_linhas_de_serie(temp_db):
    job = _rica()
    store.upsert_job(job)
    store.delete_job(job["id"])
    for t in ("job_episodes", "episode_subs", "job_torrents", "torrent_coverage",
              "torrent_files", "search_candidates"):
        assert _count(t, job["id"]) == 0, t
    # flush atrasado (create=False) de job removido não insere nada
    store.upsert_job(job, create=False)
    for t in ("job_episodes", "job_torrents", "torrent_coverage"):
        assert _count(t, job["id"]) == 0, t


def test_resumo_le_os_torrents_dos_terminais(temp_db):
    """O dropdown só lê os torrents do histórico (o % do download)."""
    job = _rica()
    job["status"] = "error"
    store.upsert_job(job)
    [j] = store.load_jobs_by_status(("error",), sections=("torrents",))
    assert "episodes" not in j and "search_candidates" not in j
    assert j["torrents"] == job["torrents"]      # campos do candidato via JOIN
    [item] = [x for x in views.summary() if x["id"] == job["id"]]
    assert item["pct"] == views._download_pct(job)


# -------------------- formato antigo --------------------

def _doc_antigo() -> dict:
    job = _rica()
    doc = {k: v for k, v in job.items()
           if k not in ("search_queries", "search_candidates")}
    doc["search_tv"] = {"original": {k: [dict(PACK, id=f"{k}:o0")]
                                     for k in job["episodes"]}, "dubbed": {}}
    doc["report"] = {"attempted": 9, "succeeded": 9, "failed": [], "skipped": []}
    # formatos que o código antigo gerava e as tabelas normalizam
    doc["episodes"]["S01E01"]["subs"]["dubbed"] = []
    doc["torrents"] = list(reversed(doc["torrents"]))
    return json.loads(json.dumps(doc))


def test_legacy_series_funcao_pura():
    doc = _doc_antigo()
    copia = json.loads(json.dumps(doc))
    out = store._legacy_series(doc)
    assert doc == copia                            # não muta a entrada
    assert "report" not in out
    assert [t["n"] for t in out["torrents"]] == [0, 1]
    assert "dubbed" not in out["episodes"]["S01E01"]["subs"]
    assert out["episodes"]["S01E02"]["pista_nova"] == {"x": 1}
    assert out["order_map"] == doc["order_map"]


def test_le_serie_no_formato_antigo_e_regrava_no_novo(temp_db):
    doc = _doc_antigo()
    with store._lock:
        store._conn.execute(
            "INSERT INTO jobs (id, created_at, status, data) VALUES (?, ?, ?, ?)",
            (doc["id"], doc["created_at"], doc["status"], json.dumps(doc)))
        store._conn.commit()
    job = store.get_job(doc["id"])
    assert "report" not in job
    assert [t["n"] for t in job["torrents"]] == [0, 1]
    assert job["episodes"]["S01E01"]["edl"] == doc["episodes"]["S01E01"]["edl"]
    assert job["order_map"] == doc["order_map"]
    # a UI recebe o relatório derivado, não o gravado
    assert jobs._public(job)["report"] is None     # S01E03 ainda pendente

    # job ativo regravado sai no formato novo, e volta igual
    store.upsert_job(job)
    data = _data(doc["id"])
    assert not any(k in data for k in ("episodes", "torrents", "order_map",
                                       "report", "search_tv"))
    back = store.get_job(doc["id"])
    for k in ("episodes", "order_map", "torrents"):
        assert back[k] == job[k], k
    # o pack do plano achou o candidato normalizado do search_tv antigo
    with store._lock:
        assert store._conn.execute(
            "SELECT cid FROM job_torrents WHERE job_id = ? AND n = 0",
            (doc["id"],)).fetchone()[0] is not None


# -------------------- relatório derivado --------------------

def test_relatorio_derivado_dos_episodios(temp_db):
    job = _serie()
    pub = jobs._public(job)
    assert pub["report"] is None                   # nada terminou
    eps = job["episodes"]
    eps["S01E01"]["state"] = "done"
    eps["S01E02"]["state"] = "failed"
    eps["S01E03"]["state"] = "review"
    eps["S01E04"]["state"] = "skipped_missing"
    assert jobs._public(job)["report"] == {
        "attempted": 3, "succeeded": 1, "failed": ["S01E02", "S01E03"],
        "skipped": ["S01E04"]}
    eps["S01E04"]["state"] = "merging"             # voltou para a fila
    assert jobs._public(job)["report"] is None
    store.upsert_job(job)
    assert "report" not in _data(job["id"])


# -------------------- id estável de magnet/link próprio --------------------

def _id_em_outro_processo(url: str, seed: str) -> str:
    code = ("import sys; from services.jobs import search; "
            "print(search.custom_candidate(sys.argv[1], '')['id'])")
    env = dict(os.environ, PYTHONHASHSEED=seed)
    out = subprocess.run([sys.executable, "-c", code, url], cwd=ROOT, env=env,
                         capture_output=True, text=True, check=True)
    return out.stdout.strip().splitlines()[-1]


def test_id_de_magnet_proprio_nao_muda_entre_processos(temp_db):
    url = "magnet:?xt=urn:btih:" + "c" * 40 + "&dn=Filme.Exemplo.2160p"
    a, b = _id_em_outro_processo(url, "1"), _id_em_outro_processo(url, "2")
    assert a == b == search.custom_candidate(url, "")["id"]
    assert pipeline._custom_candidate({"magnet": url})["id"] == a

    # o mesmo link colado de novo depois de um "restart" acha a mesma linha
    job = {"id": "mv8", "status": "awaiting", "created_at": "2026-01-01T00:00:00",
           "language": "pt", "progress": {}, "search_queries": [],
           "search_candidates": []}
    candidates.add_custom(job, "video", search.custom_candidate(url, ""))
    store.upsert_job(job)
    job = store.get_job("mv8")
    candidates.add_custom(job, "video", search.custom_candidate(url, "outro título"))
    store.upsert_job(job)
    rows = store.get_job("mv8")["search_candidates"]
    assert len(rows) == 1 and rows[0]["title"] == "outro título"


# -------------------- tamanho do documento --------------------

def test_documento_de_serie_fica_pequeno(temp_db):
    """250 episódios + torrents: o que sobra em jobs.data independe do
    tamanho da série (antes: centenas de KB, crescendo por episódio)."""
    job = _rica()
    for s in (2, 3):
        job["episodes"].update({f"S{s:02d}E{e:02d}": _ep(s, e, "done",
                                                         output=f"/dest/{s}x{e}.mkv")
                                for e in range(1, 126)})
    store.upsert_job(job)
    assert len(_data(job["id"])) < 2_000
