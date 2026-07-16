"""Nomes de pasta com [tmdbid-N] (identificação no Jellyfin) e a marcação de
pastas já existentes na coleção."""
import pytest

from services import catalog


@pytest.fixture
def item(temp_db, tmp_path):
    """Um destino com a pasta 'Ex Machina (2014)'. Devolve (dest_id, root)."""
    root = tmp_path / "colecao"
    root.mkdir()
    dest = temp_db.add_destination("T", str(root), True)
    (root / "Ex Machina (2014)").mkdir()
    return dest["id"], root


def test_folder_name():
    assert catalog.folder_name("Ex Machina", "2014", 264660) == "Ex Machina (2014) [tmdbid-264660]"
    assert catalog.folder_name("Ex Machina", "2014") == "Ex Machina (2014)"  # sem id: como sempre foi
    assert catalog.folder_name("Sem Ano", None, 5) == "Sem Ano [tmdbid-5]"
    # caracteres proibidos em nome de pasta somem (o id fica intacto)
    assert catalog.folder_name('Alien: O 8º Passageiro', "1979", 348) \
        == "Alien O 8º Passageiro (1979) [tmdbid-348]"


def test_tmdb_id_in():
    assert catalog.tmdb_id_in("Ex Machina (2014) [tmdbid-264660]") == 264660
    assert catalog.tmdb_id_in("Ex Machina (2014)") is None


def test_title_and_year_ignores_tmdbid():
    """O [tmdbid-N] não pode virar parte do título — senão o cache da coleção
    ('já tenho esse filme?') deixaria de casar com o TMDB."""
    assert catalog._title_and_year("Ex Machina (2014) [tmdbid-264660]") == ("Ex Machina", "2014")
    assert catalog._title_and_year("Sem Ano [tmdbid-5]") == ("Sem Ano", None)


def test_library_key_matches_with_tmdbid(item, temp_db):
    """Pasta marcada com o id continua casando na busca (o usuário JÁ TEM)."""
    _did, root = item
    (root / "WALL-E (2008) [tmdbid-10681]").mkdir()
    catalog.invalidate_library()
    keys = catalog.library_keys()
    assert ("walle", "2008") in keys
    assert catalog.in_library({"original_title": "WALL·E", "title": "WALL·E", "year": "2008"}, keys)


def test_tag_folder_with_tmdb(item):
    did, root = item
    novo = catalog.tag_folder_with_tmdb(did, "Ex Machina (2014)", 264660)
    assert novo == "Ex Machina (2014) [tmdbid-264660]"
    assert (root / novo).is_dir()
    assert not (root / "Ex Machina (2014)").exists()


def test_tag_folder_replaces_existing_id(item, temp_db):
    did, root = item
    (root / "Filme (2020) [tmdbid-111]").mkdir()
    novo = catalog.tag_folder_with_tmdb(did, "Filme (2020) [tmdbid-111]", 222)
    assert novo == "Filme (2020) [tmdbid-222]"
    assert (root / novo).is_dir()


def test_tag_folder_invalidates_library(item):
    did, _root = item
    catalog.library_keys()  # popula
    assert catalog._library_cache["at"] is not None
    catalog.tag_folder_with_tmdb(did, "Ex Machina (2014)", 264660)
    assert catalog._library_cache["at"] is None  # o nome da pasta mudou


def test_tag_folder_collision(item):
    did, root = item
    (root / "Ex Machina (2014) [tmdbid-264660]").mkdir()
    with pytest.raises(catalog.CatalogError, match="Já existe"):
        catalog.tag_folder_with_tmdb(did, "Ex Machina (2014)", 264660)


def test_rename_folder_rejects_escape(item):
    did, root = item
    with pytest.raises(catalog.CatalogError):
        catalog.rename_folder(did, "Ex Machina (2014)", "../fora")
    assert (root / "Ex Machina (2014)").is_dir()  # nada se moveu


def test_media_path(item, tmp_path):
    did, root = item
    folder = "Ex Machina (2014)"
    (root / folder / "filme.mkv").write_bytes(b"v")
    (root / folder / "nota.txt").write_bytes(b"t")
    assert catalog.media_path(did, folder, "filme.mkv") == root / folder / "filme.mkv"
    with pytest.raises(catalog.CatalogError, match="não é um arquivo de vídeo"):
        catalog.media_path(did, folder, "nota.txt")
    with pytest.raises(catalog.CatalogError, match="não encontrado"):
        catalog.media_path(did, folder, "sumiu.mkv")
    with pytest.raises(catalog.CatalogError):  # path traversal
        catalog.media_path(did, folder, "../../fora.mkv")
