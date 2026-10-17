"""Série "só original" / "só dublado" — o mesmo `kind` dos filmes.

Com um papel só, nada se cruza: a busca, o plano, as lacunas e a conferência
dos arquivos baixados consideram apenas aquele lado, e cada episódio é
entregue direto no destino (hardlink, ou conversão se pedida), sem merge.
"""
import asyncio
from pathlib import Path

import pytest

from services.series import merge_runner, naming, pipeline


def _job(kind: str, dest: str = "") -> dict:
    return {
        "id": "umlado01",
        "media_type": "tv",
        "tmdb_id": 4242,
        "language": "pt",
        "mode": "auto",
        "kind": kind,
        "status": "searching",
        "detail": "",
        "convert": None,
        "movie": {"original_title": "Serie Exemplo", "localized_title": "Serie Exemplo",
                  "english_title": None, "year": "2008", "poster": None,
                  "overview": None, "original_language": "en", "id": 4242},
        "request": {"seasons": [1], "episodes": {}},
        "known_seasons": [1],
        "episodes": {
            f"S01E0{e}": {"season": 1, "episode": e, "name": f"Ep {e}",
                          "air_date": "2008-01-20", "runtime": 48,
                          "state": "pending", "src": {}, "output": None,
                          "error": None}
            for e in (1, 2)},
        "torrents": [],
        "awaiting": None,
        "order_map": None,
        "report": None,
        "progress": {},
        "output": None,
        "search_tv": {"original": {}, "dubbed": {}},
        "destination_path": dest,
        "created_at": "2026-10-16T00:00:00",
    }


def _torrent(n, role, coverage):
    return {"n": n, "role": role, "tag": f"dl-umlado01-t{n}",
            "title": "Serie Exemplo S01 1080p WEB-DL", "tracker": "t",
            "seeders": 10, "size": 1000, "quality": "1080p WEB-DL",
            "coverage_label": "S01 completa", "magnet": "magnet:?xt=urn:btih:" + "a" * 40,
            "link": None, "infohash": None, "files_count": None,
            "coverage": coverage, "state": "pending", "hash": None,
            "progress": None, "selected_files": None, "content_path": None}


def test_papeis_por_kind():
    assert pipeline.roles_of({"kind": "original"}) == ("original",)
    assert pipeline.roles_of({"kind": "dubbed"}) == ("dubbed",)
    assert pipeline.roles_of({"kind": "both"}) == ("original", "dubbed")
    # jobs criados antes da opção existir carregam kind "series": os dois
    assert pipeline.roles_of({"kind": "series"}) == ("original", "dubbed")
    assert pipeline.single_role({"kind": "dubbed"}) == "dubbed"
    assert pipeline.single_role({"kind": "both"}) is None


def test_lacuna_so_conta_o_papel_pedido(temp_db):
    """Só dublado, com o dublado cobrindo tudo: o original ausente NÃO é
    lacuna — ele nunca foi pedido."""
    job = _job("dubbed")
    job["torrents"] = [_torrent(0, "dubbed", ["S01E01", "S01E02"])]
    assert pipeline._collect_gaps(job) == []
    # e o mesmo plano num job de merge acusaria o original faltando
    job["kind"] = "both"
    assert {g["episode"] for g in pipeline._collect_gaps(job)} == {"S01E01", "S01E02"}


def test_lacuna_do_papel_pedido_ainda_e_lacuna(temp_db):
    job = _job("original")
    job["torrents"] = [_torrent(0, "original", ["S01E01"])]
    assert pipeline._collect_gaps(job) == [
        {"episode": "S01E02", "name": "Ep 2", "missing": ["original"]}]


def test_plano_so_materializa_o_papel_pedido(temp_db):
    job = _job("original")
    plans = {"original": {"torrents": [
        {"title": "Serie Exemplo S01 1080p", "coverage_assigned": [(1, 1), (1, 2)]}]}}
    pipeline._store_plan(job, plans)   # sem a chave "dubbed": não pode estourar
    assert [t["role"] for t in job["torrents"]] == ["original"]
    assert job["torrents"][0]["coverage"] == ["S01E01", "S01E02"]


def test_escolha_manual_recusa_o_outro_papel(temp_db):
    job = _job("dubbed")
    with pytest.raises(ValueError, match="Papel inválido"):
        pipeline._apply_manual_torrents(
            job, {"torrents": [{"role": "original", "candidate_id": "x"}]})


def test_kind_invalido_recusado(temp_db):
    with pytest.raises(ValueError, match="kind inválido"):
        asyncio.run(pipeline.create_series(4242, "pt", seasons=[1], kind="merge"))


def test_nome_do_episodio_de_um_lado():
    assert naming.episode_file_name("Serie Exemplo", "2008", 1, 2, "pt", tag="orig") == \
        "Serie Exemplo (2008) S01E02 [orig]"
    assert naming.episode_file_name("Serie Exemplo", "2008", 1, 2, "pt", tag="pt") == \
        "Serie Exemplo (2008) S01E02 [pt]"


@pytest.mark.parametrize("kind,tag", [("original", "orig"), ("dubbed", "pt")])
def test_entrega_direta_sem_merge(temp_db, tmp_path, kind, tag):
    """Cada episódio vai direto para o layout Jellyfin por hardlink: mesmo
    arquivo (inode) do download, sem passar por ffmpeg — e o job conclui."""
    src = tmp_path / "torrent"
    src.mkdir()
    dest = tmp_path / "dest"
    job = _job(kind, str(dest))
    role = pipeline.single_role(job)
    for e in (1, 2):
        f = src / f"Serie.Exemplo.S01E0{e}.mkv"
        f.write_bytes(b"episodio " + bytes([e]))
        job["episodes"][f"S01E0{e}"].update(state="downloaded", src={role: str(f)})

    asyncio.run(merge_runner.merge_all(job))

    assert job["status"] == "done", job["detail"]
    season = dest / "Serie Exemplo (2008) [tmdbid-4242]" / "Season 01"
    for e in (1, 2):
        out = season / f"Serie Exemplo (2008) S01E0{e} [{tag}].mkv"
        assert out.is_file(), f"faltou {out}"
        assert out.stat().st_ino == (src / f"Serie.Exemplo.S01E0{e}.mkv").stat().st_ino
        assert job["episodes"][f"S01E0{e}"]["state"] == "done"
    assert job["report"]["succeeded"] == 2


def test_refazer_mantem_o_kind(temp_db, monkeypatch):
    """Recriar o job (refazer falhas) não pode virar um job de merge."""
    capturado = {}

    async def falso(*a, **kw):
        capturado.update(kw)
        return {}

    monkeypatch.setattr(pipeline, "create_series", falso)
    velho = {"tmdb_id": 4242, "language": "pt", "kind": "dubbed",
             "request": {"seasons": [1], "episodes": {}}}
    asyncio.run(pipeline.retry(velho))
    assert capturado["kind"] == "dubbed"
    # job de antes da opção existir
    velho["kind"] = "series"
    asyncio.run(pipeline.retry(velho))
    assert capturado["kind"] == "both"
