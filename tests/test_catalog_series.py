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
    _make_series(tmp_path, "Breaking Bad (2008) [tmdbid-1396]", {1: [1, 2]})
    (tmp_path / "Um Filme (2020)").mkdir()
    ((tmp_path / "Um Filme (2020)") / "Um Filme (2020) [pt+orig].mkv").write_bytes(b"x")

    items = {i["folder"]: i for i in catalog.list_items(dest_id)["items"]}
    assert items["Breaking Bad (2008) [tmdbid-1396]"]["type"] == "series"
    assert items["Um Filme (2020)"]["type"] == "movie"


def test_item_detail_agrupa_por_temporada(temp_db, tmp_path):
    dest_id = _dest(temp_db, tmp_path)
    _make_series(tmp_path, "Breaking Bad (2008) [tmdbid-1396]",
                 {1: [1, 2], 2: [1]})
    d = catalog.item_detail(dest_id, "Breaking Bad (2008) [tmdbid-1396]")
    assert d["type"] == "series"
    seasons = {sg["season"]: [f["rel"] for f in sg["files"]] for sg in d["seasons"]}
    assert sorted(seasons) == [1, 2]
    assert len(seasons[1]) == 2
    assert len(seasons[2]) == 1


def test_owned_episodes_por_tmdbid_e_por_titulo(temp_db, tmp_path):
    # o índice de séries só olha destinos de SÉRIES (bibliotecas separadas)
    dest_id = _dest(temp_db, tmp_path, media="tv")
    assert dest_id
    _make_series(tmp_path, "Breaking Bad (2008) [tmdbid-1396]",
                 {1: [1, 2, 3], 2: [5]})
    catalog.invalidate_library()

    # por id (pasta marcada)
    owned = catalog.owned_episodes(1396, None, None)
    assert owned == {1: [1, 2, 3], 2: [5]}
    # por título normalizado + ano (fallback sem id)
    owned = catalog.owned_episodes(None, "breaking bad", "2008")
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
