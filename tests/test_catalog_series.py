"""Catálogo de séries: detecção do layout Jellyfin, árvore por temporada e o
índice de episódios presentes (adicionar episódios / selo parcial)."""
from pathlib import Path

from services import catalog


def _make_series(root: Path, folder: str, seasons: dict[int, list[int]]):
    base = root / folder
    for s, eps in seasons.items():
        d = base / f"Season {s:02d}"
        d.mkdir(parents=True)
        for e in eps:
            (d / f"Show (2008) S{s:02d}E{e:02d} [pt+orig].mkv").write_bytes(b"x")
    return base


def _dest(temp_db, tmp_path, media: str = "movie") -> int:
    dests = temp_db.list_destinations_by_media(media)
    if dests:
        temp_db.update_destination(dests[0]["id"], "t", str(tmp_path), True, media)
        return dests[0]["id"]
    return temp_db.add_destination("t", str(tmp_path), True, media)["id"]


def test_list_items_detecta_serie(temp_db, tmp_path):
    dest_id = _dest(temp_db, tmp_path)
    _make_series(tmp_path, "Serie Exemplo (2008) [tmdbid-4242]", {1: [1, 2]})
    (tmp_path / "Um Filme (2020)").mkdir()
    ((tmp_path / "Um Filme (2020)") / "Um Filme (2020) [pt+orig].mkv").write_bytes(b"x")

    items = {i["folder"]: i for i in catalog.list_items(dest_id)["items"]}
    assert items["Serie Exemplo (2008) [tmdbid-4242]"]["type"] == "series"
    assert items["Um Filme (2020)"]["type"] == "movie"


def test_item_detail_agrupa_por_temporada(temp_db, tmp_path):
    dest_id = _dest(temp_db, tmp_path)
    _make_series(tmp_path, "Serie Exemplo (2008) [tmdbid-4242]",
                 {1: [1, 2], 2: [1]})
    d = catalog.item_detail(dest_id, "Serie Exemplo (2008) [tmdbid-4242]")
    assert d["type"] == "series"
    seasons = {sg["season"]: [f["rel"] for f in sg["files"]] for sg in d["seasons"]}
    assert sorted(seasons) == [1, 2]
    assert len(seasons[1]) == 2
    assert len(seasons[2]) == 1


def test_owned_episodes_por_tmdbid_e_por_titulo(temp_db, tmp_path):
    # o índice de séries só olha destinos de SÉRIES (bibliotecas separadas)
    dest_id = _dest(temp_db, tmp_path, media="tv")
    assert dest_id
    _make_series(tmp_path, "Serie Exemplo (2008) [tmdbid-4242]",
                 {1: [1, 2, 3], 2: [5]})
    catalog.invalidate_library()

    # por id (pasta marcada)
    owned = catalog.owned_episodes(4242, None, None)
    assert owned == {1: [1, 2, 3], 2: [5]}
    # por título normalizado + ano (fallback sem id)
    owned = catalog.owned_episodes(None, "serie exemplo", "2008")
    assert owned == {1: [1, 2, 3], 2: [5]}
    # série que não existe
    assert catalog.owned_episodes(999, "Outra Série", None) == {}


def test_parse_episode_filename():
    assert catalog.parse_episode_filename("Show S01E02 [pt+orig].mkv") == (1, 2)
    assert catalog.parse_episode_filename("show.s2e11.720p.mkv") == (2, 11)
    assert catalog.parse_episode_filename("Filme (2020).mkv") is None


def test_season_of_dir():
    assert catalog.season_of_dir("Season 01") == 1
    assert catalog.season_of_dir("Season 12") == 12
    assert catalog.season_of_dir("Temporada 01") is None
    assert catalog.season_of_dir("Extras") is None


def test_item_detail_de_serie_nao_roda_ffprobe(temp_db, tmp_path, monkeypatch):
    """Série grande travava a tela: item_detail sondava TODOS os episódios
    (100+ ffprobes) antes de responder. Série agora vem leve (só stat) e o
    probe roda por arquivo, sob demanda (probe_one). Filme segue completo."""
    chamadas = []
    monkeypatch.setattr(catalog, "ffprobe_json",
                        lambda p: chamadas.append(p) or {"format": {}, "streams": []})
    dest_id = _dest(temp_db, tmp_path)
    _make_series(tmp_path, "Serie Exemplo (2008) [tmdbid-4242]", {1: [1, 2, 3]})

    d = catalog.item_detail(dest_id, "Serie Exemplo (2008) [tmdbid-4242]")
    assert chamadas == []                       # nenhuma sondagem no detalhe
    f = d["seasons"][0]["files"][0]
    assert "streams" not in f and "duration" not in f
    assert f["category"] == "video" and f["size"] == 1

    # sob demanda: um arquivo, uma sondagem
    info = catalog.probe_one(dest_id, "Serie Exemplo (2008) [tmdbid-4242]", f["rel"])
    assert len(chamadas) == 1
    assert info["rel"] == f["rel"] and "streams" in info

    # filme: detalhe continua completo (poucos arquivos, sondagem barata)
    chamadas.clear()
    (tmp_path / "Um Filme (2020)").mkdir()
    ((tmp_path / "Um Filme (2020)") / "Um Filme (2020).mkv").write_bytes(b"x")
    catalog.item_detail(dest_id, "Um Filme (2020)")
    assert len(chamadas) == 1


def test_probe_one_barra_caminho_fora_da_pasta(temp_db, tmp_path):
    dest_id = _dest(temp_db, tmp_path)
    _make_series(tmp_path, "Serie Exemplo (2008) [tmdbid-4242]", {1: [1]})
    import pytest
    with pytest.raises(catalog.CatalogError):
        catalog.probe_one(dest_id, "Serie Exemplo (2008) [tmdbid-4242]",
                          "../../etc/passwd")
