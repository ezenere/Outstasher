"""Renomear arquivo do catálogo: sucesso, extensão herdada e segurança."""
from pathlib import Path

import pytest

from services import catalog


@pytest.fixture
def catalog_item(temp_db, tmp_path):
    """Um destino com um item (Ex Machina) contendo um .mkv na raiz e um .srt
    numa subpasta. Devolve (destination_id, folder, pasta_do_item)."""
    root = tmp_path / "colecao"
    root.mkdir()
    dest = temp_db.add_destination("T", str(root), True)
    item = root / "Ex Machina (2014)"
    (item / "subs").mkdir(parents=True)
    (item / "Ex Machina (2014) [pt+orig].mkv").write_bytes(b"v")
    (item / "subs" / "pt.srt").write_bytes(b"s")
    return dest["id"], "Ex Machina (2014)", item


def _names(item: Path):
    return sorted(p.relative_to(item).as_posix() for p in item.rglob("*") if p.is_file())


def test_rename_simple(catalog_item):
    did, folder, item = catalog_item
    new_rel = catalog.rename_file(did, folder, "Ex Machina (2014) [pt+orig].mkv", "Filme.mkv")
    assert new_rel == "Filme.mkv"
    assert "Filme.mkv" in _names(item)
    assert "Ex Machina (2014) [pt+orig].mkv" not in _names(item)


def test_rename_inherits_extension(catalog_item):
    did, folder, item = catalog_item
    new_rel = catalog.rename_file(did, folder, "Ex Machina (2014) [pt+orig].mkv", "Ex Machina PT")
    assert new_rel == "Ex Machina PT.mkv"
    assert "Ex Machina PT.mkv" in _names(item)


def test_rename_keeps_subfolder(catalog_item):
    did, folder, item = catalog_item
    new_rel = catalog.rename_file(did, folder, "subs/pt.srt", "portugues.srt")
    assert new_rel == "subs/portugues.srt"
    assert "subs/portugues.srt" in _names(item)


@pytest.mark.parametrize("bad", ["../fora.mkv", "a/b.mkv", "..", "", "   ", "c:on:2.mkv", "que?.mkv"])
def test_rename_rejects_unsafe_names(catalog_item, bad):
    did, folder, item = catalog_item
    before = _names(item)
    with pytest.raises(catalog.CatalogError):
        catalog.rename_file(did, folder, "Ex Machina (2014) [pt+orig].mkv", bad)
    assert _names(item) == before  # nenhum rename inválido pode ter mexido nos arquivos


def test_rename_collision(catalog_item):
    did, folder, item = catalog_item
    (item / "outro.mkv").write_bytes(b"x")
    with pytest.raises(catalog.CatalogError, match="Já existe"):
        catalog.rename_file(did, folder, "Ex Machina (2014) [pt+orig].mkv", "outro.mkv")


def test_rename_missing_source(catalog_item):
    did, folder, _ = catalog_item
    with pytest.raises(catalog.CatalogError, match="não encontrado"):
        catalog.rename_file(did, folder, "naoexiste.mkv", "x.mkv")


def test_rename_noop_same_name(catalog_item):
    did, folder, item = catalog_item
    (item / "outro.mkv").write_bytes(b"x")
    assert catalog.rename_file(did, folder, "outro.mkv", "outro.mkv") == "outro.mkv"
