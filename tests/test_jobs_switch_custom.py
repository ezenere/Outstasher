"""Troca de torrent por um magnet/link informado à mão (sem indexador)."""
import asyncio

import pytest

from services import jobs
from services.jobs import search


def test_custom_candidate_le_titulo_corte_e_qualidade():
    c = search.custom_candidate(
        "magnet:?xt=urn:btih:abc&dn=Filme+Exemplo+2014+Directors+Cut+1080p+BluRay")
    assert c["title"] == "Filme Exemplo 2014 Directors Cut 1080p BluRay"
    assert c["edition"] == "director's cut"
    assert c["quality"] == "1080p BluRay"
    assert c["tracker"] == "manual" and c["seeders"] is None
    assert c["magnet"].startswith("magnet:") and c["link"] is None
    # link http: título vem do arquivo; título explícito ganha
    c2 = search.custom_candidate("https://x/y/Filme.Exemplo.2160p.torrent")
    assert c2["title"] == "Filme.Exemplo.2160p.torrent" and c2["magnet"] is None
    assert search.custom_candidate("magnet:?xt=urn:btih:z", "Meu Título")["title"] == "Meu Título"


@pytest.mark.parametrize("url,msg", [
    ("", "magnet"),
    ("ftp://tracker/x.torrent", "magnet"),
    ("   ", "magnet"),
])
def test_custom_candidate_recusa_url_invalida(url, msg):
    with pytest.raises(ValueError, match=msg):
        search.custom_candidate(url)


def _job(status="downloading"):
    return {
        "id": "sw1", "tmdb_id": 5, "language": "pt", "mode": "auto", "kind": "both",
        "status": status, "detail": "", "movie": {"original_title": "Filme Exemplo",
                                                  "year": "2014"},
        "created_at": "2026-01-01T00:00:00", "output": None,
        "progress": {"video": None, "audio": None},
        "video_torrent": {"title": "antigo", "seeders": 9, "size": 1, "score": 1,
                          "edition": None},
        "audio_torrent": None,
        "search": {"video": [], "audio": []}, "fallbacks": {},
        "current": {"video": {"id": "v0", "title": "antigo", "seeders": 9,
                              "size": 1, "score": 1, "edition": None,
                              "magnet": "magnet:?xt=urn:btih:old"}},
    }


class FakeQbit:
    """qBittorrent de mentira? Não: um duplo que REGISTRA as chamadas — o
    cliente real exige um servidor, e o que importa aqui é o que foi pedido."""
    def __init__(self):
        self.added, self.deleted, self.untagged = [], [], []

    async def info_by_tag(self, tag):
        # só a tag de VÍDEO tem torrent: responder o mesmo hash para as duas
        # faria o código tratar como torrent compartilhado (dual áudio) e só
        # tirar a tag, em vez de apagar
        return [{"hash": "h0"}] if tag.endswith("video") else []

    async def remove_tag(self, h, tag):
        self.untagged.append((h, tag))

    async def delete(self, h, delete_files=False):
        self.deleted.append(h)

    async def add(self, url, tag, save_path=None):
        self.added.append((url, tag))


def test_switch_com_magnet_proprio(temp_db, monkeypatch):
    job = _job()
    jobs._jobs["sw1"] = job
    qbit = FakeQbit()
    monkeypatch.setattr(jobs.runtime, "_qbit", qbit)

    async def go():
        out = await jobs.switch("sw1", "video", custom={
            "url": "magnet:?xt=urn:btih:novo", "title": "Filme Exemplo 2160p Remux"})
        assert out is not None
        # o torrent novo foi para o qBittorrent e o antigo saiu
        assert qbit.added and qbit.added[0][0] == "magnet:?xt=urn:btih:novo"
        assert qbit.deleted == ["h0"]
        # virou o torrent atual e entrou na lista da busca (dá para voltar nele)
        assert job["video_torrent"]["title"] == "Filme Exemplo 2160p Remux"
        assert job["search"]["video"][0]["tracker"] == "manual"
        # o antigo virou reserva
        assert [c["id"] for c in job["fallbacks"]["video"]] == ["v0"]
    asyncio.run(go())


def test_switch_custom_valida_url_e_estado(temp_db, monkeypatch):
    job = _job(status="merging")
    jobs._jobs["sw1"] = job
    monkeypatch.setattr(jobs.runtime, "_qbit", FakeQbit())

    async def go():
        with pytest.raises(ValueError, match="não está baixando"):
            await jobs.switch("sw1", "video", custom={"url": "magnet:?xt=1"})
        job["status"] = "downloading"
        with pytest.raises(ValueError, match="magnet"):
            await jobs.switch("sw1", "video", custom={"url": "sei lá"})
        # url inválida não mexeu no job
        assert job["search"]["video"] == []
        assert job["video_torrent"]["title"] == "antigo"
    asyncio.run(go())


def test_troca_limpa_torrent_antigo_que_ficou_com_a_tag(temp_db, monkeypatch):
    """Bug de campo: sobrou torrent ANTIGO com a tag do job, então o watchdog
    lia o progresso dele e a UI nunca mostrava o novo. A troca tem que limpar
    todos os torrents da tag — e já deixar o novo no lugar, em 0%."""
    job = _job()
    job["progress"]["audio"] = {"pct": 88.0, "name": "torrent antigo",
                                "state": "downloading"}
    job["kind"] = "dubbed"                      # só áudio: nada é compartilhado
    jobs._jobs["sw1"] = job

    class DoisNaTag(FakeQbit):
        def __init__(self):
            super().__init__()
            self.tagged = [{"hash": "velho1"}, {"hash": "velho2"}]

        async def info_by_tag(self, tag):
            return self.tagged if tag.endswith("audio") else []

    qbit = DoisNaTag()
    monkeypatch.setattr(jobs.runtime, "_qbit", qbit)

    async def go():
        await jobs.switch("sw1", "audio", custom={
            "url": "magnet:?xt=urn:btih:novo", "title": "Filme Exemplo Dublado"})
        assert sorted(h for h, _ in qbit.untagged) == ["velho1", "velho2"]
        assert sorted(qbit.deleted) == ["velho1", "velho2"]
        # a UI já mostra o novo (0%, obtendo metadados), não o antigo em 88%
        p = job["progress"]["audio"]
        assert p["name"] == "Filme Exemplo Dublado" and p["pct"] == 0.0
        assert p["state"] == "metaDL"
    asyncio.run(go())


def test_troca_preserva_torrent_compartilhado_com_o_outro_papel(temp_db, monkeypatch):
    """Dual áudio: o mesmo torrent serve vídeo e áudio. Trocar o áudio tira a
    tag de áudio dele, mas NÃO o apaga — ele ainda é o vídeo."""
    job = _job()
    jobs._jobs["sw1"] = job

    class Compartilhado(FakeQbit):
        async def info_by_tag(self, tag):
            return [{"hash": "dual"}]           # o mesmo nos dois papéis

    qbit = Compartilhado()
    monkeypatch.setattr(jobs.runtime, "_qbit", qbit)

    async def go():
        await jobs.switch("sw1", "audio", custom={"url": "magnet:?xt=urn:btih:novo"})
        assert qbit.untagged == [("dual", "dl-sw1-audio")]
        assert qbit.deleted == []               # continua sendo o vídeo
    asyncio.run(go())
