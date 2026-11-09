"""Remoção de torrent que ainda serve a outro papel ou a outro job.

Caso real: o pack DUAL foi escolhido para o original E para o dublado da
série — é UM torrent no qBittorrent, com as tags dos dois torrents do plano.
O "Tentar próximo" no dublado (e a troca automática do watchdog) apagava o
torrent pelo hash, com os arquivos: o original perdia o pack inteiro no
meio do download. O mesmo valia para dois jobs pedindo o mesmo release
(409 no add = a tag do outro job no mesmo torrent) e para a limpeza do fim
do job e do cancel, de série e de filme.

A regra é uma só (jobs.downloads._release_tagged): saem as tags deste job
que não valem mais; o torrent só é apagado (com os dados) quando nenhuma
tag de outro torrent/papel do job nem de outro job o segura.

O qBittorrent é um duplo que guarda os torrents e as tags como o real (add
com o mesmo hash junta as tags no mesmo torrent) e registra as chamadas.
"""
import asyncio
import re

import httpx
import pytest

import config
from services import candidates, jobs
from services.jobs import downloads
from services.series import merge_runner, pipeline

JOB = "dual01"
SHARED = f"dl-{JOB}"


def _mag(h: str, dn: str) -> str:
    return f"magnet:?xt=urn:btih:{h * 40}&dn={dn.replace(' ', '.')}"


DUAL = ("Serie Exemplo S01 1080p WEB-DL DUAL", _mag("e", "Serie Exemplo S01 DUAL"))
DUB_NOVO = ("Serie Exemplo S01 1080p BluRay Dublado",
            _mag("d", "Serie Exemplo S01 1080p BluRay Dublado"))
ORIG_NOVO = ("Serie Exemplo S01 2160p WEB-DL",
             _mag("f", "Serie Exemplo S01 2160p WEB-DL"))


class FakeQbit:
    """Duplo do qBittorrent: torrents por hash, cada um com as suas tags."""

    def __init__(self):
        self.torrents: dict[str, dict] = {}
        self.added: list[tuple[str, str]] = []
        self.deleted: list[tuple[str, bool]] = []
        self.untagged: list[tuple[str, str]] = []
        self.offline = False

    @staticmethod
    def hash_of(url: str) -> str:
        return re.search(r"btih:([0-9a-z]+)", url).group(1)

    def _check(self):
        if self.offline:
            raise httpx.ConnectError("qBittorrent fora do ar")

    def tags(self, h: str) -> set[str]:
        return self.torrents[h]["tags"]

    async def add(self, url, tag, save_path=None):
        self._check()
        self.added.append((url, tag))
        h = self.hash_of(url)
        t = self.torrents.setdefault(h, {"hash": h, "tags": set(), "name": url})
        t["tags"] |= {x.strip() for x in tag.split(",")}

    async def info_by_tag(self, tag):
        self._check()
        return [{"hash": h, "tags": ", ".join(sorted(t["tags"])), "name": t["name"],
                 "progress": 0.4, "state": "downloading"}
                for h, t in self.torrents.items() if tag in t["tags"]]

    async def remove_tag(self, h, tags):
        self._check()
        self.untagged.append((h, tags))
        self.torrents[h]["tags"] -= {x.strip() for x in tags.split(",")}

    async def delete(self, h, delete_files=False):
        self._check()
        self.deleted.append((h, delete_files))
        self.torrents.pop(h, None)


@pytest.fixture
def qbit(monkeypatch):
    q = FakeQbit()
    monkeypatch.setattr(jobs.runtime, "_qbit", q)   # filmes e cancel
    monkeypatch.setattr(jobs, "_qbit", q)           # séries (via fachada)
    return q


# -------------------- série --------------------

def _ep(e):
    return {"season": 1, "episode": e, "name": f"Ep {e}", "air_date": "2008-01-20",
            "runtime": 48, "state": "downloading", "src": {}, "output": None,
            "error": None}


def _cand(title, magnet):
    return {"id": None, "title": title, "tracker": "x", "seeders": 5, "size": 900,
            "quality": "1080p WEB-DL", "coverage": None, "score": 1.0, "tier": 28,
            "magnet": magnet, "link": None, "infohash": None, "files": None}


def _torrent(n, role, title, magnet):
    return {"n": n, "role": role, "tag": f"{SHARED}-t{n}", "title": title,
            "tracker": "x", "seeders": 5, "size": 900, "quality": "1080p WEB-DL",
            "coverage_label": "S01 completa", "magnet": magnet, "link": None,
            "infohash": None, "files_count": None,
            "coverage": ["S01E01", "S01E02"], "state": "downloading",
            "hash": magnet.split("btih:")[1][:40], "progress": None,
            "selected_files": None, "content_path": None}


@pytest.fixture
def serie_dual(temp_db, qbit):
    """Série baixando com o MESMO pack DUAL nos dois papéis (t0 original, t1
    dublado): um torrent só no qBittorrent, com as tags t0 e t1."""
    job = {
        "id": JOB, "media_type": "tv", "tmdb_id": 4242, "language": "pt",
        "mode": "auto", "kind": "both", "status": "downloading", "detail": "",
        "movie": {"original_title": "Serie Exemplo", "localized_title": "Serie Exemplo",
                  "english_title": None, "year": "2008", "poster": None,
                  "overview": None, "original_language": "en", "id": 4242},
        "request": {"seasons": [1], "episodes": {}}, "known_seasons": [1],
        "episodes": {"S01E01": _ep(1), "S01E02": _ep(2)},
        "torrents": [_torrent(0, "original", *DUAL), _torrent(1, "dubbed", *DUAL)],
        "awaiting": None, "order_map": None, "report": None,
        "progress": {}, "output": None, "torrent_save_path": "",
        "search_queries": [], "search_candidates": [],
        "created_at": "2026-11-08T00:00:00",
    }
    for role, novo in (("original", ORIG_NOVO), ("dubbed", DUB_NOVO)):
        candidates.set_series(job, role, {k: [_cand(*DUAL), _cand(*novo)]
                                          for k in ("S01E01", "S01E02")})
    jobs._jobs[JOB] = job

    async def enviar():
        for t in job["torrents"]:
            await qbit.add(t["magnet"], f"{SHARED},{t['tag']}")
    asyncio.run(enviar())
    assert qbit.tags("e" * 40) == {SHARED, f"{SHARED}-t0", f"{SHARED}-t1"}
    yield job
    jobs._jobs.pop(JOB, None)
    pipeline._pools.pop(JOB, None)


def _id(job, role, title):
    return next(c["id"] for c in candidates.selectable(job, role)
                if c["title"] == title)


def test_trocar_o_dublado_no_pack_dual_mantem_o_pack_do_original(serie_dual, qbit):
    job = serie_dual
    asyncio.run(pipeline.switch_torrent(JOB, 1, _id(job, "dubbed", DUB_NOVO[0])))
    # o pack segue no qBittorrent servindo ao original: só a tag t1 saiu
    assert qbit.deleted == []
    assert qbit.untagged == [("e" * 40, f"{SHARED}-t1")]
    assert qbit.tags("e" * 40) == {SHARED, f"{SHARED}-t0"}
    # o dublado novo entrou com a tag t1 (a mesma entrada do plano)
    assert qbit.tags("d" * 40) == {SHARED, f"{SHARED}-t1"}
    t0, t1 = job["torrents"]
    assert t0["title"] == DUAL[0] and t0["hash"] == "e" * 40
    assert t1["title"] == DUB_NOVO[0] and t1["hash"] is None


def test_watchdog_trocando_o_original_travado_nao_apaga_o_dublado(serie_dual, qbit):
    """A troca automática do torrent travado passa pelo mesmo caminho."""
    job = serie_dual
    assert asyncio.run(pipeline._switch_stalled(job, job["torrents"][0], 30))
    assert qbit.deleted == []
    assert qbit.tags("e" * 40) == {SHARED, f"{SHARED}-t1"}
    assert qbit.tags("f" * 40) == {SHARED, f"{SHARED}-t0"}
    assert job["torrents"][0]["title"] == ORIG_NOVO[0]


def test_trocar_pack_que_outro_job_usa_tira_so_as_tags_deste(serie_dual, qbit):
    """Dois jobs da mesma série no mesmo pack: o outro job segue com ele."""
    job = serie_dual
    job["torrents"] = job["torrents"][1:]          # só o dublado usa o pack aqui
    qbit.tags("e" * 40).discard(f"{SHARED}-t0")
    qbit.tags("e" * 40).update({"dl-outro01", "dl-outro01-t4"})
    asyncio.run(pipeline.switch_torrent(JOB, 1, _id(job, "dubbed", DUB_NOVO[0])))
    assert qbit.deleted == []
    assert qbit.tags("e" * 40) == {"dl-outro01", "dl-outro01-t4"}


def test_trocar_pack_exclusivo_apaga_com_os_dados(serie_dual, qbit):
    """Comportamento de sempre: o torrent só deste torrent do plano sai do
    qBittorrent com os arquivos."""
    job = serie_dual
    job["torrents"] = job["torrents"][1:]
    qbit.tags("e" * 40).discard(f"{SHARED}-t0")
    asyncio.run(pipeline.switch_torrent(JOB, 1, None))   # "Tentar próximo"
    assert qbit.deleted == [("e" * 40, True)]
    assert set(qbit.torrents) == {"d" * 40}
    assert job["torrents"][0]["title"] == DUB_NOVO[0]


def test_qbittorrent_fora_do_ar_a_troca_nao_muda_o_plano(serie_dual, qbit):
    job = serie_dual
    antes = [dict(t) for t in job["torrents"]]
    qbit.offline = True
    with pytest.raises(ValueError, match="não respondeu"):
        asyncio.run(pipeline.switch_torrent(JOB, 1, None))
    assert job["torrents"] == antes


def test_cancelar_serie_nao_apaga_pack_de_outro_job(serie_dual, qbit):
    """Cancel com "remover torrents": o pack DUAL deste job sai inteiro (as
    duas tags são dele); o que outro job também usa fica, sem as tags deste."""
    outro = _mag("c", "Serie Exemplo S02")
    asyncio.run(qbit.add(outro, f"{SHARED},{SHARED}-t2,dl-outro01,dl-outro01-t0"))
    asyncio.run(jobs.cancel(JOB, delete_torrents=True))
    assert qbit.deleted == [("e" * 40, True)]
    assert qbit.tags("c" * 40) == {"dl-outro01", "dl-outro01-t0"}


def test_limpeza_do_fim_da_serie_nao_apaga_pack_de_outro_job(serie_dual, qbit,
                                                             monkeypatch):
    monkeypatch.setattr(config, "QBIT_CLEANUP", "remove_data")
    outro = _mag("c", "Serie Exemplo S02")
    asyncio.run(qbit.add(outro, f"{SHARED},{SHARED}-t2,dl-outro01,dl-outro01-t0"))
    asyncio.run(merge_runner._cleanup_torrents(serie_dual))
    assert qbit.deleted == [("e" * 40, True)]
    assert qbit.tags("c" * 40) == {"dl-outro01", "dl-outro01-t0"}


# -------------------- filme --------------------

MV = "film01"


@pytest.fixture
def filme(temp_db, qbit):
    """Filme kind both baixando; o vídeo ativo é v0."""
    job = {
        "id": MV, "tmdb_id": 5, "language": "pt", "mode": "auto", "kind": "both",
        "status": "downloading", "detail": "",
        "movie": {"original_title": "Filme Exemplo", "year": "2014"},
        "created_at": "2026-01-01T00:00:00", "output": None,
        "progress": {"video": None, "audio": None}, "search_queries": [],
        "search_candidates": [candidates.new(
            "video", "v0", {"title": "Filme Exemplo 1080p DUAL", "seeders": 9,
                            "size": 1, "score": 1, "edition": None,
                            "magnet": "magnet:?xt=urn:btih:" + "a" * 40},
            active=True)],
    }
    jobs._jobs[MV] = job
    yield job
    jobs._jobs.pop(MV, None)


NOVO = {"id": "v9", "title": "Filme Exemplo 2160p", "seeders": 3, "size": 1,
        "score": 1, "edition": None, "magnet": "magnet:?xt=urn:btih:" + "b" * 40}


def test_filme_troca_de_torrent_que_outro_job_usa_tira_so_a_tag(filme, qbit):
    asyncio.run(qbit.add("magnet:?xt=urn:btih:" + "a" * 40,
                         f"dl-{MV}-video,dl-outro02-video"))
    asyncio.run(downloads._replace_torrent(filme, "video", None, NOVO, "troca"))
    assert qbit.deleted == []
    assert qbit.tags("a" * 40) == {"dl-outro02-video"}
    assert qbit.tags("b" * 40) == {f"dl-{MV}-video"}


def test_filme_troca_de_torrent_exclusivo_apaga_com_os_dados(filme, qbit):
    asyncio.run(qbit.add("magnet:?xt=urn:btih:" + "a" * 40, f"dl-{MV}-video"))
    asyncio.run(downloads._replace_torrent(filme, "video", None, NOVO, "troca"))
    assert qbit.deleted == [("a" * 40, True)]


def test_filme_dual_trocar_o_video_mantem_o_audio(filme, qbit):
    asyncio.run(qbit.add("magnet:?xt=urn:btih:" + "a" * 40,
                         f"dl-{MV}-video,dl-{MV}-audio"))
    asyncio.run(downloads._replace_torrent(filme, "video", None, NOVO, "troca"))
    assert qbit.deleted == []
    assert qbit.tags("a" * 40) == {f"dl-{MV}-audio"}


def test_filme_cancelar_mantem_torrent_de_outro_job(filme, qbit):
    """O DUAL deste job sai uma vez (as duas tags são dele); o que outro job
    também usa fica."""
    asyncio.run(qbit.add("magnet:?xt=urn:btih:" + "a" * 40,
                         f"dl-{MV}-video,dl-{MV}-audio"))
    asyncio.run(qbit.add("magnet:?xt=urn:btih:" + "c" * 40,
                         f"dl-{MV}-audio,dl-outro02-audio"))
    asyncio.run(jobs.cancel(MV, delete_torrents=True))
    assert qbit.deleted == [("a" * 40, True)]
    assert qbit.tags("c" * 40) == {"dl-outro02-audio"}


def test_filme_limpeza_do_fim_mantem_torrent_de_outro_job(filme, qbit, monkeypatch):
    monkeypatch.setattr(config, "QBIT_CLEANUP", "remove_data")
    asyncio.run(qbit.add("magnet:?xt=urn:btih:" + "a" * 40,
                         f"dl-{MV}-video,dl-outro02-video"))
    asyncio.run(qbit.add("magnet:?xt=urn:btih:" + "c" * 40, f"dl-{MV}-audio"))
    asyncio.run(jobs.delivery._cleanup_torrents(filme))
    assert qbit.deleted == [("c" * 40, True)]
    assert qbit.tags("a" * 40) == {"dl-outro02-video"}
