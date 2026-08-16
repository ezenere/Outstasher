"""Merge por episódio de séries: entrega Jellyfin, falha parcial e regra dos 75%.

Os testes de merge usam MKVs REAIS (make_media) no atalho de hardlink: o
arquivo "original" já carrega o áudio alvo, então o merger entrega por
hardlink sem re-encode — caminho determinístico e rápido, que ainda exercita
nomeação, layout de pastas e o laço por episódio de verdade.
"""
import asyncio

import pytest

from services.series import merge_runner, naming


def _job(dest: str) -> dict:
    return {
        "id": "serietest1",
        "media_type": "tv",
        "tmdb_id": 1396,
        "language": "pt",
        "mode": "auto",
        "status": "downloading",
        "detail": "",
        "convert": None,
        "movie": {"original_title": "Breaking Bad", "localized_title": "Breaking Bad",
                  "english_title": None, "year": "2008", "poster": None,
                  "overview": None, "original_language": "en", "id": 1396},
        "episodes": {},
        "torrents": [],
        "awaiting": None,
        "report": None,
        "progress": {},
        "output": None,
        "destination_path": dest,
        "created_at": "2026-08-16T00:00:00",
    }


def _ep(season, episode, state="downloaded", src=None):
    return {"season": season, "episode": episode, "name": f"Ep {episode}",
            "air_date": "2008-01-20", "runtime": 48, "state": state,
            "src": src or {}, "output": None, "error": None}


# -------------------- naming --------------------

def test_naming_layout_jellyfin():
    assert naming.series_folder_name("Breaking Bad", "2008", 1396) == \
        "Breaking Bad (2008) [tmdbid-1396]"
    assert naming.season_dir_name(1) == "Season 01"
    assert naming.episode_file_name("Breaking Bad", "2008", 1, 2, "pt") == \
        "Breaking Bad (2008) S01E02 [pt+orig]"


# -------------------- relatório / regra dos 75% --------------------

def test_75_rule_aborta(temp_db, tmp_path):
    job = _job(str(tmp_path))
    job["episodes"] = {
        "S01E01": _ep(1, 1, "done"),
        "S01E02": _ep(1, 2, "done"),
        "S01E03": _ep(1, 3, "failed"),
        "S01E04": _ep(1, 4, "failed"),
    }
    merge_runner._finish(job)  # 2/4 = 50% < 75%
    assert job["status"] == "error"
    assert job["report"]["succeeded"] == 2
    assert job["report"]["failed"] == ["S01E03", "S01E04"]


def test_75_rule_no_limite_passa(temp_db, tmp_path):
    job = _job(str(tmp_path))
    job["episodes"] = {
        "S01E01": _ep(1, 1, "done"),
        "S01E02": _ep(1, 2, "done"),
        "S01E03": _ep(1, 3, "done"),
        "S01E04": _ep(1, 4, "failed"),
    }
    merge_runner._finish(job)  # 3/4 = 75%: não é MENOS que 75% -> conclui
    assert job["status"] == "done"
    assert "falha" in job["detail"]


def test_skipped_fora_do_denominador(temp_db, tmp_path):
    job = _job(str(tmp_path))
    job["episodes"] = {
        "S01E01": _ep(1, 1, "done"),
        # pulados por decisão (lacuna/estreia futura) não contam como tentados
        "S01E02": _ep(1, 2, "skipped_missing"),
        "S01E03": _ep(1, 3, "skipped_future"),
    }
    merge_runner._finish(job)  # 1/1 = 100%
    assert job["status"] == "done"
    assert job["report"]["attempted"] == 1
    assert job["report"]["skipped"] == ["S01E02", "S01E03"]


def test_nada_tentado_falha(temp_db, tmp_path):
    job = _job(str(tmp_path))
    job["episodes"] = {"S01E01": _ep(1, 1, "skipped_missing")}
    merge_runner._finish(job)
    assert job["status"] == "error"


# -------------------- merge real por episódio --------------------

@pytest.mark.ffmpeg
def test_merge_all_entrega_layout_jellyfin(temp_db, tmp_path, make_media):
    """2 episódios bons: o original já tem o áudio alvo (atalho de hardlink) —
    saída no layout Série (Ano) [tmdbid]/Season NN/... e job concluído."""
    src = tmp_path / "src"
    src.mkdir()
    dest = tmp_path / "dest"
    job = _job(str(dest))
    for e in (1, 2):
        orig = make_media(src / f"orig.S01E0{e}.mkv", ["eng", "por"], w=1280, h=536)
        dub = make_media(src / f"dub.S01E0{e}.mkv", ["por"])
        job["episodes"][f"S01E0{e}"] = _ep(1, e, src={
            "original": str(orig), "dubbed": str(dub)})

    asyncio.run(merge_runner.merge_all(job))

    assert job["status"] == "done", job["detail"]
    season = dest / "Breaking Bad (2008) [tmdbid-1396]" / "Season 01"
    for e in (1, 2):
        out = season / f"Breaking Bad (2008) S01E0{e} [pt+orig].mkv"
        assert out.is_file(), f"faltou {out}"
        assert job["episodes"][f"S01E0{e}"]["state"] == "done"
    assert job["report"]["succeeded"] == 2


@pytest.mark.ffmpeg
def test_merge_all_falha_parcial_continua(temp_db, tmp_path, make_media):
    """Episódio com arquivo sumido falha SOZINHO; os demais seguem. Com 3/4 de
    sucesso (75%), o job conclui listando a falha para re-tentar depois."""
    src = tmp_path / "src"
    src.mkdir()
    dest = tmp_path / "dest"
    job = _job(str(dest))
    for e in (1, 2, 3):
        orig = make_media(src / f"orig.S01E0{e}.mkv", ["eng", "por"], w=1280, h=536)
        dub = make_media(src / f"dub.S01E0{e}.mkv", ["por"])
        job["episodes"][f"S01E0{e}"] = _ep(1, e, src={
            "original": str(orig), "dubbed": str(dub)})
    # o E04 aponta para arquivos que não existem -> MergeError só dele
    job["episodes"]["S01E04"] = _ep(1, 4, src={
        "original": str(src / "nao-existe-orig.mkv"),
        "dubbed": str(src / "nao-existe-dub.mkv")})

    asyncio.run(merge_runner.merge_all(job))

    assert job["status"] == "done", job["detail"]
    assert job["episodes"]["S01E04"]["state"] == "failed"
    assert job["report"] == {"attempted": 4, "succeeded": 3,
                             "failed": ["S01E04"], "skipped": []}
    for e in (1, 2, 3):
        assert job["episodes"][f"S01E0{e}"]["state"] == "done"
