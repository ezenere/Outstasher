"""Mecânica dos gates e utilitários de estado do pipeline de séries.

Sem rede: os testes constroem o dict do job diretamente (o mesmo shape que
create_series/_prepare produzem) e exercitam as funções de estado — lacunas,
force_continue, escolha manual, mapeamento de arquivo por episódio.
"""
from pathlib import Path

import pytest

from services.series import pipeline


def _job(tmp_path: Path | None = None) -> dict:
    """Job de série mínimo no shape do pipeline (2 episódios pedidos)."""
    return {
        "id": "testjob01",
        "media_type": "tv",
        "tmdb_id": 1396,
        "language": "pt",
        "mode": "auto",
        "status": "searching",
        "detail": "",
        "movie": {"original_title": "Breaking Bad", "localized_title": "Breaking Bad",
                  "english_title": None, "year": "2008", "poster": None,
                  "overview": None, "original_language": "en", "id": 1396},
        "request": {"seasons": [1], "episodes": {}},
        "known_seasons": [1, 2],
        "episodes": {
            "S01E01": {"season": 1, "episode": 1, "name": "Pilot",
                       "air_date": "2008-01-20", "runtime": 58,
                       "state": "pending", "src": {}, "output": None, "error": None},
            "S01E02": {"season": 1, "episode": 2, "name": "Cat's in the Bag...",
                       "air_date": "2008-01-27", "runtime": 48,
                       "state": "pending", "src": {}, "output": None, "error": None},
        },
        "torrents": [],
        "awaiting": None,
        "order_map": None,
        "report": None,
        "progress": {},
        "output": None,
        "search_tv": {"original": {}, "dubbed": {}},
        "created_at": "2026-08-16T00:00:00",
    }


def _torrent(n, role, coverage, title="Breaking Bad S01 1080p WEB-DL"):
    return {"n": n, "role": role, "tag": f"dl-testjob01-t{n}", "title": title,
            "tracker": "t", "seeders": 10, "size": 1000, "quality": "1080p WEB-DL",
            "coverage_label": "S01 completa", "magnet": f"magnet:?xt=urn:btih:{'a' * 40}",
            "link": None, "infohash": None, "files_count": None,
            "coverage": coverage, "state": "pending", "hash": None,
            "progress": None, "selected_files": None, "content_path": None}


# -------------------- lacunas --------------------

def test_collect_gaps_por_papel(temp_db):
    job = _job()
    # original cobre os dois; dublado só o E01 -> E02 é lacuna do dublado
    job["torrents"] = [
        _torrent(0, "original", ["S01E01", "S01E02"]),
        _torrent(1, "dubbed", ["S01E01"], "Breaking Bad S01E01 Dublado"),
    ]
    gaps = pipeline._collect_gaps(job)
    assert gaps == [{"episode": "S01E02", "name": "Cat's in the Bag...",
                     "missing": ["dublado"]}]


def test_apply_gaps_pula_e_enxuga_plano(temp_db):
    job = _job()
    job["torrents"] = [
        _torrent(0, "original", ["S01E01", "S01E02"]),
        _torrent(1, "dubbed", ["S01E01"], "Breaking Bad S01E01 Dublado"),
        # torrent que SÓ existia pelo episódio que virou lacuna
        _torrent(2, "original", ["S01E02"], "Breaking Bad S01E02 720p"),
    ]
    pipeline._apply_gaps(job)
    assert job["episodes"]["S01E02"]["state"] == "skipped_missing"
    assert job["episodes"]["S01E01"]["state"] == "pending"
    # o E02 saiu de todas as coberturas; o torrent 2 sumiu do plano
    assert [t["n"] for t in job["torrents"]] == [0, 1]
    assert job["torrents"][0]["coverage"] == ["S01E01"]
    # sem mais lacunas depois de aceitar
    assert pipeline._collect_gaps(job) == []


def test_skipped_future_fica_fora_das_lacunas(temp_db):
    job = _job()
    job["episodes"]["S01E02"]["state"] = "skipped_future"
    job["torrents"] = [
        _torrent(0, "original", ["S01E01"]),
        _torrent(1, "dubbed", ["S01E01"], "Breaking Bad S01E01 Dublado"),
    ]
    assert pipeline._collect_gaps(job) == []


# -------------------- force_continue / resolução de arquivos --------------------

def test_force_continue_abandona_e_pula(temp_db, tmp_path):
    job = _job()
    job["force_continue"] = True
    done = _torrent(0, "original", ["S01E01", "S01E02"])
    done.update(state="done", content_path=str(tmp_path))
    stuck = _torrent(1, "dubbed", ["S01E01", "S01E02"], "BB S01 Dublado")
    stuck["state"] = "downloading"
    job["torrents"] = [done, stuck]
    (tmp_path / "Breaking.Bad.S01E01.1080p.mkv").write_bytes(b"x" * 10)
    (tmp_path / "Breaking.Bad.S01E02.1080p.mkv").write_bytes(b"x" * 10)

    pipeline._apply_force_continue(job)
    assert stuck["state"] == "abandoned"

    import asyncio
    asyncio.run(pipeline._resolve_episode_files(job))
    # original achado, dublado abandonado -> episódios PULADOS (não falhos)
    assert job["episodes"]["S01E01"]["state"] == "skipped_missing"
    assert job["episodes"]["S01E02"]["state"] == "skipped_missing"


def test_resolve_episode_files_completo(temp_db, tmp_path):
    job = _job()
    orig_dir = tmp_path / "orig"
    dub_dir = tmp_path / "dub"
    orig_dir.mkdir()
    dub_dir.mkdir()
    for d in (orig_dir, dub_dir):
        (d / "Breaking.Bad.S01E01.mkv").write_bytes(b"x" * 10)
        (d / "Breaking.Bad.S01E02.mkv").write_bytes(b"x" * 10)
    t0 = _torrent(0, "original", ["S01E01", "S01E02"])
    t0.update(state="done", content_path=str(orig_dir))
    t1 = _torrent(1, "dubbed", ["S01E01", "S01E02"], "BB S01 Dublado")
    t1.update(state="done", content_path=str(dub_dir))
    job["torrents"] = [t0, t1]

    import asyncio
    asyncio.run(pipeline._resolve_episode_files(job))
    for key in ("S01E01", "S01E02"):
        ep = job["episodes"][key]
        assert ep["state"] == "downloaded"
        assert set(ep["src"]) == {"original", "dubbed"}


# -------------------- _find_episode_file --------------------

def test_find_episode_file_por_nome(tmp_path):
    (tmp_path / "Show.S01E01.720p.mkv").write_bytes(b"x" * 5)
    big = tmp_path / "Show.S01E02.1080p.mkv"
    big.write_bytes(b"x" * 100)
    (tmp_path / "Show.S01E02.720p.mkv").write_bytes(b"x" * 50)
    (tmp_path / "sample.S01E02.mkv").write_bytes(b"x" * 999)  # sample: ignora
    # dois matches do E02 (720p/1080p) -> fica o maior; sample nunca
    assert pipeline._find_episode_file(tmp_path, 1, 2) == big


def test_find_episode_file_arquivo_unico_sem_ref(tmp_path):
    only = tmp_path / "Show.Pilot.mkv"
    only.write_bytes(b"x" * 5)
    assert pipeline._find_episode_file(tmp_path, 1, 1) == only


def test_find_episode_file_ambiguo_falha(tmp_path):
    (tmp_path / "a.mkv").write_bytes(b"x")
    (tmp_path / "b.mkv").write_bytes(b"x")
    with pytest.raises(RuntimeError, match="S01E03"):
        pipeline._find_episode_file(tmp_path, 1, 3)


# -------------------- escolha manual --------------------

def test_apply_manual_agrupa_pack(temp_db):
    job = _job()
    job["torrents"] = [
        _torrent(0, "original", ["S01E01", "S01E02"]),
        _torrent(1, "dubbed", ["S01E01", "S01E02"], "BB S01 Dublado WEB-DL"),
    ]
    pack = {"id": "S01E01:d0", "title": "Breaking Bad S01 BluRay Dublado",
            "tracker": "x", "seeders": 5, "size": 900, "quality": "1080p BluRay",
            "coverage": "S01 completa", "score": 1.0, "tier": 29,
            "magnet": "magnet:?xt=urn:btih:" + "b" * 40, "link": None,
            "infohash": None, "files": None}
    pack2 = dict(pack, id="S01E02:d0")
    job["search_tv"]["dubbed"] = {"S01E01": [pack], "S01E02": [pack2]}

    pipeline._apply_manual(job, {"picks": {
        "S01E01": {"dubbed": "S01E01:d0"},
        "S01E02": {"dubbed": "S01E02:d0"},
    }})
    dubbed = [t for t in job["torrents"] if t["role"] == "dubbed"]
    # o mesmo pack escolhido para os 2 episódios vira UM torrent; o antigo saiu
    assert len(dubbed) == 1
    assert dubbed[0]["title"] == "Breaking Bad S01 BluRay Dublado"
    assert dubbed[0]["coverage"] == ["S01E01", "S01E02"]
    # o lado original não foi tocado
    orig = [t for t in job["torrents"] if t["role"] == "original"]
    assert orig[0]["coverage"] == ["S01E01", "S01E02"]


def test_apply_manual_candidato_invalido(temp_db):
    job = _job()
    with pytest.raises(ValueError, match="não encontrado"):
        pipeline._apply_manual(job, {"picks": {"S01E01": {"dubbed": "nope"}}})


# -------------------- escolha manual invertida (torrent -> episódios) ------

def _pack_cand(cid, title, **kw):
    base = {"id": cid, "title": title, "tracker": "x", "seeders": 5,
            "size": 900, "quality": "1080p WEB-DL", "coverage": None,
            "score": 1.0, "tier": 28,
            "magnet": "magnet:?xt=urn:btih:" + ("%040x" % abs(hash(title))),
            "link": None, "infohash": None, "files": None}
    base.update(kw)
    return base


def test_manual_torrents_auto_atribui_pelo_titulo(temp_db):
    job = _job()
    job["torrents"] = [
        _torrent(0, "original", ["S01E01", "S01E02"]),
        _torrent(1, "dubbed", ["S01E01", "S01E02"], "BB S01 Dublado"),
    ]
    pack = _pack_cand("S01E01:d0", "Breaking Bad S01 BluRay Dublado")
    job["search_tv"]["dubbed"] = {"S01E01": [pack]}
    pipeline._apply_manual(job, {"torrents": [
        {"candidate_id": "S01E01:d0", "role": "dubbed", "episodes": "auto"},
    ]})
    dubbed = [t for t in job["torrents"] if t["role"] == "dubbed"]
    # o papel marcado foi SUBSTITUÍDO pela seleção; "auto" cobriu S01E01+02
    # (o título "S01" cobre a temporada); o original ficou intocado
    assert len(dubbed) == 1
    assert dubbed[0]["title"] == "Breaking Bad S01 BluRay Dublado"
    assert dubbed[0]["coverage"] == ["S01E01", "S01E02"]
    orig = [t for t in job["torrents"] if t["role"] == "original"]
    assert orig[0]["coverage"] == ["S01E01", "S01E02"]


def test_manual_torrents_explicito_vence_auto(temp_db):
    job = _job()
    job["torrents"] = [_torrent(0, "dubbed", ["S01E01", "S01E02"], "auto plano")]
    pack = _pack_cand("a", "Breaking Bad S01 WEB-DL Dublado")
    avulso = _pack_cand("b", "Breaking Bad S01E02 BluRay Dublado")
    job["search_tv"]["dubbed"] = {"S01E01": [pack, avulso]}
    pipeline._apply_manual(job, {"torrents": [
        {"candidate_id": "a", "role": "dubbed", "episodes": "auto"},
        # E02 explícito no avulso: sai do auto do pack
        {"candidate_id": "b", "role": "dubbed", "episodes": ["S01E02"]},
    ]})
    by_title = {t["title"]: t["coverage"] for t in job["torrents"]
                if t["role"] == "dubbed"}
    assert by_title["Breaking Bad S01 WEB-DL Dublado"] == ["S01E01"]
    assert by_title["Breaking Bad S01E02 BluRay Dublado"] == ["S01E02"]


def test_manual_torrents_titulo_sem_match_e_aceito(temp_db):
    # atribuição que o TÍTULO não indica é decisão explícita do usuário
    job = _job()
    job["torrents"] = []
    cand = _pack_cand("a", "Breaking Bad 1080p Coletânea Dublado")
    job["search_tv"]["dubbed"] = {"S01E01": [cand]}
    pipeline._apply_manual(job, {"torrents": [
        {"candidate_id": "a", "role": "dubbed", "episodes": ["S01E01", "S01E02"]},
    ]})
    dubbed = [t for t in job["torrents"] if t["role"] == "dubbed"]
    assert dubbed[0]["coverage"] == ["S01E01", "S01E02"]


def test_manual_torrents_episodio_fora_do_pedido(temp_db):
    job = _job()
    cand = _pack_cand("a", "Breaking Bad S01 Dublado")
    job["search_tv"]["dubbed"] = {"S01E01": [cand]}
    with pytest.raises(ValueError, match="fora do pedido"):
        pipeline._apply_manual(job, {"torrents": [
            {"candidate_id": "a", "role": "dubbed", "episodes": ["S09E09"]},
        ]})


# -------------------- troca manual de torrent --------------------

def test_find_replacement_mesma_cobertura(temp_db):
    job = _job()
    stalled = _torrent(0, "original", ["S01E01", "S01E02"])
    job["torrents"] = [stalled]
    reserva_boa = _pack_cand("r1", "Breaking Bad S01 720p WEB-DL")
    reserva_parcial = _pack_cand("r2", "Breaking Bad S01E01 1080p")
    # a parcial vem primeiro no rank, mas não cobre S01E02 -> pula
    job["search_tv"]["original"] = {"S01E01": [reserva_parcial, reserva_boa]}
    nxt = pipeline._find_replacement(job, stalled)
    assert nxt["id"] == "r1"


def test_switch_torrent_valida_estado_e_cobertura(temp_db):
    import asyncio
    job = _job()
    job["status"] = "downloading"
    t = _torrent(0, "original", ["S01E01", "S01E02"])
    t["state"] = "downloading"
    job["torrents"] = [t]
    parcial = _pack_cand("p", "Breaking Bad S01E01 1080p")
    job["search_tv"]["original"] = {"S01E01": [parcial]}
    import services.jobs as jobs_mod
    jobs_mod._jobs[job["id"]] = job
    try:
        # candidato que não cobre tudo é recusado com a lista do que falta
        with pytest.raises(ValueError, match="S01E02"):
            asyncio.run(pipeline.switch_torrent(job["id"], 0, "p"))
        # sem reserva compatível, "tentar próximo" explica
        with pytest.raises(ValueError, match="reserva"):
            asyncio.run(pipeline.switch_torrent(job["id"], 0, None))
        # torrent inexistente
        with pytest.raises(ValueError, match="t9"):
            asyncio.run(pipeline.switch_torrent(job["id"], 9, None))
    finally:
        jobs_mod._jobs.pop(job["id"], None)


# -------------------- gate de lacunas -> editar seleção --------------------

def test_gaps_edit_volta_para_o_manual(temp_db):
    import asyncio
    import services.jobs as jobs_mod
    job = _job()
    job["mode"] = "auto"
    job["status"] = "awaiting"
    job["awaiting"] = {"reason": "gaps_confirm", "payload": {"missing": []}}
    job["torrents"] = [_torrent(0, "original", ["S01E01"])]
    jobs_mod._jobs[job["id"]] = job

    async def go():
        await pipeline.resolve(job["id"], "gaps_confirm", {"edit": True})
        task = jobs_mod._tasks.get(job["id"])
        if task:
            await task

    try:
        asyncio.run(go())
        assert job["mode"] == "manual"
        assert job["status"] == "awaiting"
        assert job["awaiting"]["reason"] == "manual_pick"
        # a visão invertida vai no payload do gate reaberto
        assert "by_torrent" in job["awaiting"]["payload"]
    finally:
        jobs_mod._jobs.pop(job["id"], None)


# -------------------- validação de temporadas pedidas --------------------

def test_temporada_0_especiais_e_aceita():
    # regressão: a UI lista "Especiais" (S0) e o job pedia [0,1,2] — a
    # validação rejeitava a própria seleção oferecida (job 513180aed1)
    all_seasons = {0, 1, 2}
    pipeline._check_requested_seasons(all_seasons, {"seasons": [0, 1, 2],
                                                    "episodes": {}})
    pipeline._check_requested_seasons(all_seasons, {"seasons": [],
                                                    "episodes": {0: [1]}})


def test_temporada_inexistente_rejeita():
    with pytest.raises(ValueError, match="S09"):
        pipeline._check_requested_seasons({0, 1, 2}, {"seasons": [1, 9],
                                                      "episodes": {}})


# -------------------- order_map (remap de ordem alternativa) --------------------

def test_search_ref_com_order_map(temp_db):
    job = _job()
    job["order_map"] = {"S01E02": [1, 5]}  # na ordem do BD, o E02 é o 5º
    assert pipeline._search_ref(job, "S01E02") == (1, 5)
    assert pipeline._search_ref(job, "S01E01") == (1, 1)
    assert pipeline._aired_ref(job, (1, 5)) == (1, 2)
    assert pipeline._aired_ref(job, (1, 1)) == (1, 1)


# -------------------- match fino pelos arquivos do pack --------------------

def test_match_pack_files_revela_o_que_o_pack_tem(temp_db):
    # regressão real: "1ª Temporada Completa" com o ª corrompido pelo Jackett
    # foi lido como série completa e recebeu S01-S04; os ARQUIVOS só tinham S01
    job = _job()
    job["episodes"]["S02E01"] = dict(job["episodes"]["S01E01"], season=2, episode=1)
    t = _torrent(0, "dubbed", ["S01E01", "S01E02", "S02E01"],
                 "Mr Robot 2016   1�� Temporada Completa [WEB DL] BLUDV")
    files = [
        {"index": 0, "name": "Pack/EP01/Mr.Robot.S01E01.720p.DUAL.mkv"},
        {"index": 1, "name": "Pack/EP01/Mr.Robot.S01E01.720p.DUAL.srt"},
        {"index": 2, "name": "Pack/EP02/Mr.Robot.S01E02.720p.DUAL.mkv"},
        {"index": 3, "name": "Pack/extras/making-of.mkv"},
    ]
    keep, drop, found = pipeline._match_pack_files(job, t, files)
    assert found == {"S01E01", "S01E02"}          # S02E01 NÃO está no pack
    assert [f["index"] for f in keep] == [0, 1, 2]  # vídeo + legenda dos pedidos
    assert [f["index"] for f in drop] == [3]


def test_parse_ordinal_corrompido_e_temporada_nao_serie_completa():
    from services.series import parse
    c = parse.parse_coverage("Mr Robot 2016   1���� Temporada Completa [WEB DL] BLUDV")
    assert c.kind == "season_pack" and c.seasons == {1}
    c = parse.parse_coverage("Mr Robot 2016 1ª Temporada Completa [WEB-DL] BLUDV")
    assert c.kind == "season_pack" and c.seasons == {1}


def test_apply_gaps_mantem_torrent_ja_baixado(temp_db):
    job = _job()
    done = _torrent(0, "dubbed", ["S01E02"], "pack que só tinha o E02")
    done["state"] = "done"
    pend = _torrent(1, "original", ["S01E02"])  # pendente e só por esse ep
    job["torrents"] = [done, pend]
    # E01 sem ninguém -> vira skipped; E02 continua coberto
    pipeline._apply_gaps(job)
    assert job["episodes"]["S01E01"]["state"] == "skipped_missing"
    assert [t["n"] for t in job["torrents"]] == [0, 1]


def test_manual_torrents_mid_download_preserva_o_que_ja_baixou(temp_db):
    job = _job()
    done = _torrent(0, "dubbed", ["S01E01"], "BB S01E01 Dublado")
    done["state"] = "done"
    job["torrents"] = [done]
    cand = _pack_cand("x", "Breaking Bad S01E02 WEB-DL Dublado")
    job["search_tv"]["dubbed"] = {"S01E02": [cand]}
    pipeline._apply_manual(job, {"torrents": [
        {"candidate_id": "x", "role": "dubbed", "episodes": ["S01E02"]},
    ]})
    by_n = {t["n"]: t for t in job["torrents"]}
    assert by_n[0]["state"] == "done" and by_n[0]["coverage"] == ["S01E01"]
    assert any(t["coverage"] == ["S01E02"] and t["state"] == "pending"
               for t in job["torrents"])
