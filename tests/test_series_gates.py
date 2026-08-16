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


# -------------------- order_map (remap de ordem alternativa) --------------------

def test_search_ref_com_order_map(temp_db):
    job = _job()
    job["order_map"] = {"S01E02": [1, 5]}  # na ordem do BD, o E02 é o 5º
    assert pipeline._search_ref(job, "S01E02") == (1, 5)
    assert pipeline._search_ref(job, "S01E01") == (1, 1)
    assert pipeline._aired_ref(job, (1, 5)) == (1, 2)
    assert pipeline._aired_ref(job, (1, 1)) == (1, 1)
