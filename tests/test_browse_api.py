"""Navegador de pastas: o que a UI usa para escolher qualquer caminho."""
import pytest

from services import browse


def test_lista_pastas_e_arquivos_de_midia(tmp_path):
    (tmp_path / "Season 01").mkdir()
    (tmp_path / "Season 02").mkdir()
    (tmp_path / "filme.mkv").write_bytes(b"x" * 10)
    (tmp_path / "leia-me.txt").write_text("nao aparece")
    (tmp_path / ".oculto").mkdir()

    d = browse.list_dir(str(tmp_path))
    assert [x["name"] for x in d["dirs"]] == ["Season 01", "Season 02"]
    assert [x["name"] for x in d["files"]] == ["filme.mkv"]
    assert d["files"][0]["size"] == 10
    assert d["parent"] == str(tmp_path.parent)
    assert d["truncated"] is False


def test_erro_claro_em_caminho_ruim(tmp_path):
    with pytest.raises(browse.BrowseError, match="não existe"):
        browse.list_dir(str(tmp_path / "nada"))
    arq = tmp_path / "a.mkv"
    arq.write_bytes(b"x")
    with pytest.raises(browse.BrowseError, match="não é uma pasta"):
        browse.list_dir(str(arq))


def test_raiz_nao_tem_pai():
    assert browse.list_dir("/")["parent"] is None


def test_atalhos_saem_dos_destinos_cadastrados(temp_db, tmp_path):
    col = tmp_path / "colecao"
    col.mkdir()
    temp_db.add_destination("Filmes", str(col), True)
    atalhos = browse.shortcuts()
    assert any(a["path"] == str(col) and "Filmes" in a["label"] for a in atalhos)
    # a navegação começa num atalho, não em /
    assert browse.default_path() == atalhos[0]["path"]
