"""Cache 'já está na coleção': scan on demand, TTL de 30 min e invalidações."""
import pytest

from services import catalog, jobs


@pytest.fixture
def library(temp_db, tmp_path):
    """Dois destinos com pastas de filme (e um arquivo solto que não conta).
    Começa sem os destinos-padrão semeados pelo init."""
    for d in temp_db.list_destinations():
        temp_db.delete_destination(d["id"])
    root1, root2 = tmp_path / "a", tmp_path / "b"
    root1.mkdir()
    root2.mkdir()
    temp_db.add_destination("A", str(root1), True)
    temp_db.add_destination("B", str(root2), False)
    (root1 / "Ex Machina (2014)").mkdir()
    (root1 / "Tóquio Proibida (1999)").mkdir()
    (root2 / "WALL-E (2008)").mkdir()
    (root2 / "Sem Ano").mkdir()
    (root2 / "arquivo_solto.txt").write_bytes(b"x")  # não é pasta: fica de fora
    return root1, root2


def test_norm_title(temp_db):
    assert catalog._norm_title("WALL·E") == "walle"
    assert catalog._norm_title("Tóquio Proibida") == "toquioproibida"
    assert catalog._norm_title("Ex_Machina: Instinto!") == "exmachinainstinto"


def test_cache_holds_until_invalidated(library):
    root1, _ = library
    keys = catalog.library_keys()
    assert ("exmachina", "2014") in keys and ("walle", "2008") in keys
    assert ("semano", None) in keys
    assert len(keys) == 4, keys
    # mudança no disco NÃO aparece até invalidar
    (root1 / "Novo Filme (2020)").mkdir()
    assert ("novofilme", "2020") not in catalog.library_keys()
    catalog.invalidate_library()
    assert ("novofilme", "2020") in catalog.library_keys()  # rescan on demand


def test_ttl_triggers_rescan(library):
    root1, _ = library
    catalog.library_keys()  # popula o cache
    (root1 / "Outro (2021)").mkdir()
    assert ("outro", "2021") not in catalog.library_keys()  # cache ainda válido
    catalog._library_cache["at"] -= catalog.LIBRARY_TTL_SECONDS + 1  # envelhece 30 min
    assert ("outro", "2021") in catalog.library_keys()


def test_in_library_matching(library):
    keys = catalog.library_keys()
    assert catalog.in_library({"original_title": "Ex Machina", "title": "Ex Machina: Instinto Artificial", "year": "2014"}, keys)
    assert catalog.in_library({"original_title": "Original X", "title": "Tóquio Proibida", "year": "1999"}, keys)  # pelo localizado
    assert catalog.in_library({"original_title": "WALL·E", "title": "WALL·E", "year": "2008"}, keys)  # pontuação difere
    assert catalog.in_library({"original_title": "Sem Ano", "title": None, "year": "1987"}, keys)     # pasta sem ano
    assert not catalog.in_library({"original_title": "Ex Machina", "title": None, "year": "2033"}, keys)  # ano errado
    assert not catalog.in_library({"original_title": "Inexistente", "title": None, "year": "2014"}, keys)


def test_invalidation_on_job_done_and_delete_folder(library):
    root1, _ = library
    (root1 / "Novo Filme (2020)").mkdir()
    catalog.library_keys()
    assert catalog._library_cache["at"] > 0

    # um job que conclui invalida o cache (entrou filme novo no destino)
    job = {"id": "x1", "tmdb_id": 1, "language": "pt", "mode": "auto", "kind": "both",
           "status": "merging", "detail": "", "movie": None,
           "created_at": "2026-01-01T00:00:00", "video_torrent": None,
           "audio_torrent": None, "output": None,
           "progress": {"video": None, "audio": None},
           "search": None, "fallbacks": None, "current": None}
    jobs._jobs["x1"] = job
    jobs._set(job, "done", "ok")
    assert catalog._library_cache["at"] == 0.0

    # remover uma pasta também invalida
    catalog.library_keys()
    catalog.delete_folder(None, "Novo Filme (2020)")
    assert catalog._library_cache["at"] == 0.0
    assert ("novofilme", "2020") not in catalog.library_keys()


def test_unmounted_destination_does_not_break_scan(library, temp_db, tmp_path):
    # destino apontando para um caminho inexistente não pode derrubar o scan
    temp_db.add_destination("C", str(tmp_path / "nao" / "existe"), False)
    catalog.invalidate_library()
    assert ("exmachina", "2014") in catalog.library_keys()
