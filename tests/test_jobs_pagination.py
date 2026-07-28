"""Paginação da lista de jobs (list_group).

Os jobs terminais paginam no SQL; 'active'/'all' envolvem a memória e paginam
depois de montar a lista. Ambos os caminhos são exercitados aqui com o store
real (fixture temp_db).
"""
from services import jobs


def _mk(store, i: int, status: str) -> dict:
    """Grava um job terminal direto no banco (created_at crescente com i)."""
    job = {
        "id": f"job{i:03d}", "tmdb_id": 100 + i, "language": "pt",
        "kind": "both", "status": status,
        "created_at": f"2026-01-{i + 1:02d}T00:00:00",
        "detail": "", "movie": None, "progress": {}, "convert": None,
    }
    store.upsert_job(job)
    return job


def _mem(i: int, status: str) -> dict:
    """Job ativo, que vive em memória (_jobs) e não no banco."""
    job = {
        "id": f"mem{i:03d}", "tmdb_id": 200 + i, "language": "pt",
        "kind": "both", "status": status,
        "created_at": f"2026-02-{i + 1:02d}T00:00:00",
        "detail": "", "movie": None, "progress": {}, "convert": None,
    }
    jobs._jobs[job["id"]] = job
    return job


def test_sem_per_page_devolve_tudo(temp_db):
    for i in range(5):
        _mk(temp_db, i, "done")
    r = jobs.list_group("done")
    assert len(r["items"]) == 5
    assert r["total"] == 5 and r["pages"] == 1 and r["page"] == 1


def test_pagina_grupo_terminal_no_sql(temp_db):
    for i in range(12):
        _mk(temp_db, i, "done")

    p1 = jobs.list_group("done", page=1, per_page=5)
    assert len(p1["items"]) == 5
    assert p1["total"] == 12 and p1["pages"] == 3 and p1["page"] == 1

    p3 = jobs.list_group("done", page=3, per_page=5)
    assert len(p3["items"]) == 2  # sobra da última página

    # sem sobreposição entre as páginas e ordem decrescente por created_at
    ids = [j["id"] for j in p1["items"] + jobs.list_group("done", 2, 5)["items"]
           + p3["items"]]
    assert len(set(ids)) == 12
    assert ids == sorted(ids, reverse=True)


def test_pagina_grupo_active_da_memoria(temp_db):
    for i in range(7):
        _mem(i, "downloading")
    r = jobs.list_group("active", page=2, per_page=3)
    assert len(r["items"]) == 3
    assert r["total"] == 7 and r["pages"] == 3


def test_all_junta_memoria_e_banco(temp_db):
    for i in range(4):
        _mk(temp_db, i, "done")
    for i in range(3):
        _mem(i, "downloading")
    r = jobs.list_group("all", page=1, per_page=10)
    assert r["total"] == 7
    assert len(r["items"]) == 7
    # os ativos são mais recentes (fevereiro) e vêm primeiro
    assert [j["id"] for j in r["items"]][:3] == ["mem002", "mem001", "mem000"]


def test_pagina_alem_do_fim_volta_vazia(temp_db):
    for i in range(3):
        _mk(temp_db, i, "done")
    r = jobs.list_group("done", page=9, per_page=5)
    assert r["items"] == []
    assert r["pages"] == 1  # o cliente usa isso para voltar à última página


def test_grupo_invalido(temp_db):
    import pytest
    with pytest.raises(ValueError):
        jobs.list_group("naoexiste")
