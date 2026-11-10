"""Editar a seleção de uma série com torrents JÁ no qBittorrent.

Caso real: o pack dublado só tinha parte dos episódios que o título
prometia; o watchdog abriu o aviso de lacunas no meio do download e o
"Editar seleção" levava a uma escolha VAZIA. O usuário marcava só o que
faltava e o job juntava tudo — o pack antigo seguia baixando ao lado do
novo. Agora a escolha abre com o plano atual pré-selecionado (inclusive o
que já baixa) e a confirmação SUBSTITUI o plano: torrent mantido não é
reenviado, o desmarcado sai do plano e do qBittorrent, o novo entra.

O qBittorrent é um duplo que guarda os torrents e as tags como o real
(add com o mesmo hash = 409 + tag nova no mesmo torrent) e registra as
chamadas; o watchdog e o resolve rodam de verdade.
"""
import asyncio
import re

import httpx
import pytest

from services import candidates, jobs
from services.series import pipeline

JOB = "edit01"
SHARED = f"dl-{JOB}"


def _mag(h: str, dn: str) -> str:
    return f"magnet:?xt=urn:btih:{h * 40}&dn={dn.replace(' ', '.')}"


ORIG = ("Serie Exemplo S01 1080p WEB-DL", _mag("a", "Serie Exemplo S01 1080p WEB-DL"))
DUB = ("Serie Exemplo S01 720p WEB-DL Dublado",
       _mag("b", "Serie Exemplo S01 720p WEB-DL Dublado"))
DUB_E02 = ("Serie Exemplo S01E02 1080p WEB-DL Dublado",
           _mag("c", "Serie Exemplo S01E02 1080p WEB-DL Dublado"))
DUB_NOVO = ("Serie Exemplo S01 1080p BluRay Dublado",
            _mag("d", "Serie Exemplo S01 1080p BluRay Dublado"))


class FakeQbit:
    """Duplo do qBittorrent: torrents por hash, cada um com as suas tags."""

    def __init__(self, files: dict[str, list[dict]] | None = None):
        self.torrents: dict[str, dict] = {}
        self.files_by_hash = files or {}
        self.added: list[tuple[str, str]] = []
        self.deleted: list[tuple[str, bool]] = []
        self.untagged: list[tuple[str, str]] = []
        self.prio: list[tuple[str, list[int], int]] = []
        self.offline = False

    @staticmethod
    def hash_of(url: str) -> str:
        return re.search(r"btih:([0-9a-z]+)", url).group(1)

    def _check(self):
        if self.offline:
            raise httpx.ConnectError("qBittorrent fora do ar")

    async def add(self, url, tag, save_path=None):
        self._check()
        self.added.append((url, tag))
        h = self.hash_of(url)
        t = self.torrents.setdefault(h, {"hash": h, "tags": set(), "name": url,
                                         "progress": 0.4})
        t["tags"] |= {x.strip() for x in tag.split(",")}

    async def info_by_tag(self, tag):
        self._check()
        return [{"hash": h, "tags": ", ".join(sorted(t["tags"])),
                 "name": t["name"], "progress": t["progress"],
                 "state": "downloading", "dlspeed": 1, "num_seeds": 3,
                 "size": 10, "completed": 4}
                for h, t in self.torrents.items() if tag in t["tags"]]

    async def files(self, h):
        return self.files_by_hash.get(h, [])

    async def file_prio(self, h, ids, prio):
        self.prio.append((h, list(ids), prio))

    async def remove_tag(self, h, tags):
        self._check()
        self.untagged.append((h, tags))
        self.torrents[h]["tags"] -= {x.strip() for x in tags.split(",")}

    async def delete(self, h, delete_files=False):
        self._check()
        self.deleted.append((h, delete_files))
        self.torrents.pop(h, None)


def _ep(e):
    return {"season": 1, "episode": e, "name": f"Ep {e}", "air_date": "2008-01-20",
            "runtime": 48, "state": "pending", "src": {}, "output": None,
            "error": None}


def _job() -> dict:
    return {
        "id": JOB, "media_type": "tv", "tmdb_id": 4242, "language": "pt",
        "mode": "auto", "kind": "both", "status": "searching", "detail": "",
        "movie": {"original_title": "Serie Exemplo", "localized_title": "Serie Exemplo",
                  "english_title": None, "year": "2008", "poster": None,
                  "overview": None, "original_language": "en", "id": 4242},
        "request": {"seasons": [1], "episodes": {}}, "known_seasons": [1],
        "episodes": {"S01E01": _ep(1), "S01E02": _ep(2)},
        "torrents": [], "awaiting": None, "order_map": None, "report": None,
        "progress": {}, "output": None, "torrent_save_path": "",
        "search_queries": [], "search_candidates": [],
        "created_at": "2026-11-08T00:00:00",
    }


def _cand(cid, title, magnet):
    return {"id": cid, "title": title, "tracker": "x", "seeders": 5, "size": 900,
            "quality": "1080p WEB-DL", "coverage": None, "score": 1.0, "tier": 28,
            "magnet": magnet, "link": None, "infohash": None, "files": None}


def _torrent(n, role, title, magnet, coverage):
    return {"n": n, "role": role, "tag": f"{SHARED}-t{n}", "title": title,
            "tracker": "x", "seeders": 5, "size": 900, "quality": "1080p WEB-DL",
            "coverage_label": "S01 completa", "magnet": magnet, "link": None,
            "infohash": None, "files_count": None, "coverage": coverage,
            "state": "pending", "hash": None, "progress": None,
            "selected_files": None, "content_path": None}


def _files(*names):
    return [{"index": i, "name": n, "priority": 1} for i, n in enumerate(names)]


@pytest.fixture
def em_lacuna(temp_db, monkeypatch):
    """Job no aviso de lacunas ABERTO PELO WATCHDOG: os dois packs já estão no
    qBittorrent e os arquivos do dublado mostraram que ele não tem o E02."""
    qbit = FakeQbit(files={
        "a" * 40: _files("Pack/Serie.Exemplo.S01E01.1080p.mkv",
                         "Pack/Serie.Exemplo.S01E02.1080p.mkv"),
        "b" * 40: _files("Pack/Serie.Exemplo.S01E01.720p.Dublado.mkv",
                         "Pack/extras/making-of.mkv"),
    })
    monkeypatch.setattr(jobs, "_qbit", qbit)
    job = _job()
    candidates.set_series(job, "original", {"S01E01": [_cand(None, *ORIG)],
                                            "S01E02": [_cand(None, *ORIG)]})
    candidates.set_series(job, "dubbed", {
        "S01E01": [_cand(None, *DUB), _cand(None, *DUB_NOVO)],
        "S01E02": [_cand(None, *DUB), _cand(None, *DUB_NOVO), _cand(None, *DUB_E02)],
    })
    job["torrents"] = [_torrent(0, "original", *ORIG, ["S01E01", "S01E02"]),
                       _torrent(1, "dubbed", *DUB, ["S01E01", "S01E02"])]
    jobs._jobs[JOB] = job
    asyncio.run(pipeline._start_downloads(job))
    yield job, qbit
    jobs._jobs.pop(JOB, None)
    pipeline._pools.pop(JOB, None)


def _resolve(job, reason, decision):
    async def go():
        out = await pipeline.resolve(job["id"], reason, decision)
        task = jobs._tasks.get(job["id"])
        if task:
            await task
        return out
    return asyncio.run(go())


def _parar_no_download(monkeypatch):
    """A continuação depois da escolha para no watchdog (o que importa é o
    que foi ENVIADO ao qBittorrent)."""
    chegou = []

    async def watchdog(job):
        chegou.append([t["n"] for t in job["torrents"]])
    monkeypatch.setattr(pipeline, "_wait_downloads", watchdog)
    return chegou


def _abrir_edicao(job):
    _resolve(job, "gaps_confirm", {"edit": True})
    assert job["awaiting"]["reason"] == "manual_pick"
    return jobs._public(job)["awaiting"]["payload"]


def _eventos(job):
    from services import store
    jobs.flush_dirty(force=True)
    return store.load_events(job["id"])


def _cobertura_unica(job):
    """Nunca dois torrents do job no mesmo episódio×papel."""
    vistos = set()
    for t in job["torrents"]:
        for k in t["coverage"]:
            assert (t["role"], k) not in vistos, f"{k} duplicado em {t['role']}"
            vistos.add((t["role"], k))


# -------------------- o cenário de partida --------------------

def test_watchdog_abre_lacuna_com_os_packs_no_qbittorrent(em_lacuna):
    job, qbit = em_lacuna
    assert job["status"] == "awaiting"
    assert job["awaiting"]["reason"] == "gaps_confirm"
    payload = jobs._public(job)["awaiting"]["payload"]
    assert payload["mid_download"] is True
    assert [g["episode"] for g in payload["missing"]] == ["S01E02"]
    # o E02 não serve mais: os ARQUIVOS desmentiram o título
    dub = job["torrents"][1]
    assert dub["coverage"] == ["S01E01"] and dub["coverage_shrunk"] == ["S01E02"]
    assert len(qbit.added) == 2
    # a edição já começa daqui: buscar de novo apagaria downloads em curso
    assert payload["can_research"] is False


# -------------------- pré-seleção --------------------

def test_edicao_abre_com_o_plano_em_andamento_pre_selecionado(em_lacuna):
    job, _ = em_lacuna
    payload = _abrir_edicao(job)
    pre = {p["n"]: p for p in payload["preselected"]}
    assert set(pre) == {0, 1}
    assert pre[0]["role"] == "original" and pre[0]["coverage"] == ["S01E01", "S01E02"]
    # o dublado vem com a cobertura que os arquivos CONFIRMARAM (sem o E02)
    assert pre[1]["role"] == "dubbed" and pre[1]["coverage"] == ["S01E01"]
    assert {p["state"] for p in pre.values()} == {"downloading"}
    # cada pré-selecionado aponta para a linha que o representa na lista,
    # que vem no topo, marcada como do plano
    for p in pre.values():
        lst = payload["by_torrent"][p["role"]]
        linha = next(c for c in lst if c["id"] == p["candidate_id"])
        assert linha["planned"]["n"] == p["n"] and lst[0] is linha
        assert linha["planned"]["state"] == "downloading"
    # o "auto" do pack dublado não devolve o E02 que os arquivos não têm
    dub_linha = payload["by_torrent"]["dubbed"][0]
    assert dub_linha["title"] == DUB[0] and dub_linha["matches"] == ["S01E01"]


# -------------------- confirmar substitui, não duplica --------------------

def test_completar_lacuna_nao_reenvia_os_mantidos(em_lacuna, monkeypatch):
    job, qbit = em_lacuna
    payload = _abrir_edicao(job)
    chegou = _parar_no_download(monkeypatch)
    e02 = next(c for c in payload["by_torrent"]["dubbed"] if c["title"] == DUB_E02[0])
    # a UI devolve o plano pré-selecionado + o torrent novo para a lacuna
    sel = [{"candidate_id": p["candidate_id"], "role": p["role"],
            "episodes": p["coverage"]} for p in payload["preselected"]]
    sel.append({"candidate_id": e02["id"], "role": "dubbed", "episodes": "auto"})
    _resolve(job, "manual_pick", {"torrents": sel, "replace_plan": True})

    assert job["status"] == "downloading" and chegou
    by_n = {t["n"]: t for t in job["torrents"]}
    # mantidos: mesma entrada (tag, hash, estado) — nada reenviado
    assert by_n[0]["state"] == "downloading" and by_n[0]["hash"] == "a" * 40
    assert by_n[1]["state"] == "downloading" and by_n[1]["coverage"] == ["S01E01"]
    # o novo cobre só a lacuna e foi o ÚNICO enviado agora
    assert by_n[2]["title"] == DUB_E02[0] and by_n[2]["coverage"] == ["S01E02"]
    assert [FakeQbit.hash_of(u) for u, _ in qbit.added] == ["a" * 40, "b" * 40, "c" * 40]
    assert qbit.added[-1][1] == f"{SHARED},{SHARED}-t2"
    assert qbit.deleted == []
    _cobertura_unica(job)


def test_trocar_o_pack_remove_o_antigo_do_qbittorrent(em_lacuna, monkeypatch):
    """O caso real: o usuário escolhe um pack dublado melhor. O antigo (que
    já baixava) sai do plano e do qBittorrent, com os dados; o novo entra
    sozinho no papel — antes os dois seguiam baixando."""
    job, qbit = em_lacuna
    payload = _abrir_edicao(job)
    _parar_no_download(monkeypatch)
    novo = next(c for c in payload["by_torrent"]["dubbed"] if c["title"] == DUB_NOVO[0])
    orig = next(p for p in payload["preselected"] if p["role"] == "original")
    _resolve(job, "manual_pick", {"replace_plan": True, "torrents": [
        {"candidate_id": orig["candidate_id"], "role": "original",
         "episodes": orig["coverage"]},
        # o pack dublado antigo foi DESMARCADO
        {"candidate_id": novo["id"], "role": "dubbed", "episodes": "auto"},
    ]})

    assert qbit.deleted == [("b" * 40, True)]
    assert "b" * 40 not in qbit.torrents
    dub = [t for t in job["torrents"] if t["role"] == "dubbed"]
    assert len(dub) == 1 and dub[0]["title"] == DUB_NOVO[0]
    assert dub[0]["coverage"] == ["S01E01", "S01E02"] and dub[0]["n"] == 2
    # o original mantido não foi reenviado; o novo, sim
    assert [FakeQbit.hash_of(u) for u, _ in qbit.added] == ["a" * 40, "b" * 40, "d" * 40]
    _cobertura_unica(job)
    msgs = [e["message"] for e in _eventos(job)]
    assert any("removido do qBittorrent com os dados" in m for m in msgs)


def test_qbittorrent_fora_do_ar_mantem_gate_e_plano(em_lacuna):
    job, qbit = em_lacuna
    payload = _abrir_edicao(job)
    antes = [dict(t) for t in job["torrents"]]
    novo = next(c for c in payload["by_torrent"]["dubbed"] if c["title"] == DUB_NOVO[0])
    qbit.offline = True
    with pytest.raises(ValueError, match="não respondeu"):
        _resolve(job, "manual_pick", {"replace_plan": True, "torrents": [
            {"candidate_id": novo["id"], "role": "dubbed", "episodes": "auto"}]})
    # nada aplicado: o gate continua de pé e o plano é o de antes
    assert job["status"] == "awaiting" and job["awaiting"]["reason"] == "manual_pick"
    assert job["torrents"] == antes


# -------------------- regras da substituição (sem o watchdog) --------------------

def _job_planejado():
    job = _job()
    job["status"] = "awaiting"
    candidates.set_series(job, "dubbed", {
        "S01E01": [_cand(None, *DUB), _cand(None, *DUB_NOVO)],
        "S01E02": [_cand(None, *DUB), _cand(None, *DUB_NOVO)]})
    return job


def test_dois_auto_no_mesmo_episodio_o_primeiro_fica(temp_db):
    job = _job_planejado()
    ids = {c["title"]: c["id"] for c in candidates.selectable(job, "dubbed")}
    plan, removed, _ = pipeline._plan_manual_torrents(job, {
        "replace_plan": True, "torrents": [
            {"candidate_id": ids[DUB_NOVO[0]], "role": "dubbed", "episodes": "auto"},
            {"candidate_id": ids[DUB[0]], "role": "dubbed", "episodes": "auto"}]})
    # os dois packs cobrem a temporada pelo título: só o primeiro entra
    assert [(t["title"], t["coverage"]) for t in plan] == [
        (DUB_NOVO[0], ["S01E01", "S01E02"])]
    assert removed == []


def test_replace_sem_marca_alguma_e_recusado(temp_db):
    job = _job_planejado()
    with pytest.raises(ValueError, match="Nenhum torrent marcado"):
        pipeline._plan_manual_torrents(job, {"replace_plan": True, "torrents": []})


def test_pendente_mantido_vira_a_mesma_entrada_e_desmarcado_some(temp_db):
    job = _job_planejado()
    job["torrents"] = [_torrent(0, "original", *ORIG, ["S01E01", "S01E02"]),
                       _torrent(1, "dubbed", *DUB, ["S01E01", "S01E02"])]
    ids = {c["title"]: c["id"] for c in candidates.selectable(job, "dubbed")}
    removed = pipeline._apply_manual(job, {"replace_plan": True, "torrents": [
        # o original não é candidato da busca: volta pelo id do plano
        {"candidate_id": "plan:0", "role": "original", "episodes": ["S01E01", "S01E02"]},
        {"candidate_id": ids[DUB_NOVO[0]], "role": "dubbed", "episodes": "auto"}]})
    assert removed == []                     # o desmarcado nunca foi enviado
    assert [(t["n"], t["title"]) for t in job["torrents"]] == [
        (0, ORIG[0]), (2, DUB_NOVO[0])]


def test_pack_compartilhado_perde_so_a_tag(em_lacuna, monkeypatch):
    """O mesmo torrent servindo a outro job (409 no add = a tag do outro no
    mesmo torrent): sair do plano deste job tira só as tags dele."""
    job, qbit = em_lacuna
    qbit.torrents["b" * 40]["tags"].add("dl-outrojob-t3")
    payload = _abrir_edicao(job)
    _parar_no_download(monkeypatch)
    novo = next(c for c in payload["by_torrent"]["dubbed"] if c["title"] == DUB_NOVO[0])
    orig = next(p for p in payload["preselected"] if p["role"] == "original")
    _resolve(job, "manual_pick", {"replace_plan": True, "torrents": [
        {"candidate_id": orig["candidate_id"], "role": "original",
         "episodes": orig["coverage"]},
        {"candidate_id": novo["id"], "role": "dubbed", "episodes": "auto"}]})
    assert qbit.deleted == []
    assert qbit.torrents["b" * 40]["tags"] == {"dl-outrojob-t3"}


async def _enviar(job):
    """Envia os pendentes ao qBittorrent como o _start_downloads, sem o
    watchdog."""
    for t in job["torrents"]:
        await jobs._qbit.add(t["magnet"], f"{SHARED},{t['tag']}")
        t["state"] = "downloading"


def test_dual_nos_dois_papeis_desmarcar_um_nao_apaga_o_outro(temp_db, monkeypatch):
    """Pack DUAL usado nos dois papéis: é UM torrent no qBittorrent com as
    tags t0 e t1. Desmarcar o dublado não pode apagar o original."""
    qbit = FakeQbit()
    monkeypatch.setattr(jobs, "_qbit", qbit)
    job = _job_planejado()
    dual = ("Serie Exemplo S01 1080p WEB-DL DUAL", _mag("e", "Serie Exemplo S01 DUAL"))
    job["torrents"] = [_torrent(0, "original", *dual, ["S01E01", "S01E02"]),
                       _torrent(1, "dubbed", *dual, ["S01E01", "S01E02"])]
    jobs._jobs[JOB] = job
    try:
        asyncio.run(_enviar(job))
        assert qbit.torrents["e" * 40]["tags"] == {SHARED, f"{SHARED}-t0", f"{SHARED}-t1"}
        ids = {c["title"]: c["id"] for c in candidates.selectable(job, "dubbed")}
        plan, removed, _ = pipeline._plan_manual_torrents(job, {
            "replace_plan": True, "torrents": [
                {"candidate_id": "plan:0", "role": "original",
                 "episodes": ["S01E01", "S01E02"]},
                {"candidate_id": ids[DUB_NOVO[0]], "role": "dubbed",
                 "episodes": "auto"}]})
        assert [t["n"] for t in removed] == [1]
        asyncio.run(pipeline._drop_sent(job, removed, plan))
        assert qbit.deleted == []
        assert qbit.torrents["e" * 40]["tags"] == {SHARED, f"{SHARED}-t0"}
    finally:
        jobs._jobs.pop(JOB, None)


# -------------------- pack mantido que ganha episódio --------------------

def test_pack_mantido_que_ganha_episodio_religa_os_arquivos(temp_db, monkeypatch):
    """O E02 estava com um avulso; o usuário tira o avulso e dá o E02 ao pack
    que já baixava — o arquivo do E02 tinha sido DESLIGADO (prioridade 0)
    pela seleção anterior e precisa voltar a baixar, mesmo com o pack
    marcado como concluído."""
    qbit = FakeQbit(files={"b" * 40: [
        {"index": 0, "name": "Pack/Serie.Exemplo.S01E01.mkv", "priority": 1},
        {"index": 1, "name": "Pack/Serie.Exemplo.S01E02.mkv", "priority": 0}]})
    monkeypatch.setattr(jobs, "_qbit", qbit)
    job = _job_planejado()
    pack = _torrent(0, "dubbed", *DUB, ["S01E01"])
    pack.update(state="done", hash="b" * 40, content_path="/x/Pack",
                selected_files=["Pack/Serie.Exemplo.S01E01.mkv"])
    avulso = _torrent(1, "dubbed", *DUB_E02, ["S01E02"])
    job["torrents"] = [pack, avulso]
    ids = {c["title"]: c["id"] for c in candidates.selectable(job, "dubbed")}
    pipeline._apply_manual(job, {"replace_plan": True, "torrents": [
        {"candidate_id": ids[DUB[0]], "role": "dubbed", "episodes": "auto"}]})
    t = job["torrents"][0]
    assert [x["n"] for x in job["torrents"]] == [0]
    assert t["coverage"] == ["S01E01", "S01E02"]
    # volta para o watchdog, que refaz a seleção dos arquivos
    assert t["state"] == "downloading" and t["selected_files"] is None
    assert asyncio.run(pipeline._ensure_file_selection(job, t)) is True
    assert ("b" * 40, [1], 1) in qbit.prio
    assert t["selected_files"] == ["Pack/Serie.Exemplo.S01E01.mkv",
                                   "Pack/Serie.Exemplo.S01E02.mkv"]


def test_pack_baixando_que_perde_episodio_desliga_o_arquivo(temp_db, monkeypatch):
    """O E02 passa do pack (ainda baixando) para um avulso: o pack não pode
    seguir baixando o E02 em paralelo — a seleção de arquivos é refeita."""
    qbit = FakeQbit(files={"b" * 40: _files("Pack/Serie.Exemplo.S01E01.mkv",
                                             "Pack/Serie.Exemplo.S01E02.mkv")})
    monkeypatch.setattr(jobs, "_qbit", qbit)
    job = _job_planejado()
    candidates.set_series(job, "dubbed", {"S01E02": [_cand(None, *DUB_E02)]})
    pack = _torrent(0, "dubbed", *DUB, ["S01E01", "S01E02"])
    pack.update(state="downloading", hash="b" * 40,
                selected_files=["Pack/Serie.Exemplo.S01E01.mkv",
                                "Pack/Serie.Exemplo.S01E02.mkv"])
    job["torrents"] = [pack]
    avulso = candidates.selectable(job, "dubbed")[0]
    removed = pipeline._apply_manual(job, {"replace_plan": True, "torrents": [
        {"candidate_id": "plan:0", "role": "dubbed", "episodes": ["S01E01"]},
        {"candidate_id": avulso["id"], "role": "dubbed", "episodes": "auto"}]})
    assert removed == []
    t = job["torrents"][0]
    assert t["coverage"] == ["S01E01"] and t["selected_files"] is None
    assert asyncio.run(pipeline._ensure_file_selection(job, t)) is False
    assert ("b" * 40, [1], 0) in qbit.prio          # o E02 do pack desligado
    _cobertura_unica(job)


# -------------------- editar/remover DURANTE o download --------------------
# Pedido do usuário: em série, "trocar" não faz sentido (uns vêm por
# episódio, outros em pack). Durante o download: remover qualquer torrent e
# abrir a escolha para adicionar qualquer resultado (de qualquer papel) ou
# magnet próprio. Os pedidos são atendidos pelo watchdog no tick seguinte.

@pytest.fixture
def baixando(temp_db, monkeypatch):
    qbit = FakeQbit(files={
        "a" * 40: _files("Pack/Serie.Exemplo.S01E01.1080p.mkv",
                         "Pack/Serie.Exemplo.S01E02.1080p.mkv"),
        "b" * 40: _files("Pack/Serie.Exemplo.S01E01.720p.Dublado.mkv",
                         "Pack/Serie.Exemplo.S01E02.720p.Dublado.mkv"),
    })
    monkeypatch.setattr(jobs, "_qbit", qbit)
    monkeypatch.setattr(jobs, "poll_interval", lambda: 0.01)
    job = _job()
    candidates.set_series(job, "original", {"S01E01": [_cand(None, *ORIG)],
                                            "S01E02": [_cand(None, *ORIG)]})
    candidates.set_series(job, "dubbed", {
        "S01E01": [_cand(None, *DUB), _cand(None, *DUB_NOVO)],
        "S01E02": [_cand(None, *DUB), _cand(None, *DUB_NOVO), _cand(None, *DUB_E02)],
    })
    job["torrents"] = [_torrent(0, "original", *ORIG, ["S01E01", "S01E02"]),
                       _torrent(1, "dubbed", *DUB, ["S01E01", "S01E02"])]
    jobs._jobs[JOB] = job
    yield job, qbit
    jobs._jobs.pop(JOB, None)
    pipeline._pools.pop(JOB, None)


def _durante_o_download(job, reason, decision):
    """Envia, espera o watchdog entrar, faz o pedido e espera ele atender."""
    async def go():
        task = asyncio.create_task(pipeline._start_downloads(job))
        for _ in range(500):
            await asyncio.sleep(0.01)
            if job["status"] == "downloading":
                break
        assert job["status"] == "downloading"
        await pipeline.resolve(job["id"], reason, decision)
        await asyncio.wait_for(task, 5)
    asyncio.run(go())


def test_remover_um_torrent_durante_o_download(baixando):
    job, qbit = baixando
    _durante_o_download(job, "remove_torrent", {"torrent_n": 1})
    # saiu do qBittorrent (com os dados: nenhum outro torrent o usa) e do plano
    assert qbit.deleted == [("b" * 40, True)]
    assert [t["n"] for t in job["torrents"]] == [0]
    # o dublado ficou sem cobertura: o aviso de lacunas de sempre decide
    assert job["status"] == "awaiting"
    assert job["awaiting"]["reason"] == "gaps_confirm"
    msgs = [e["message"] for e in _eventos(job)]
    assert any("t1 removido do plano" in m for m in msgs)


def test_editar_torrents_reabre_a_escolha_com_o_plano(baixando):
    job, qbit = baixando
    _durante_o_download(job, "edit_torrents", {})
    assert job["status"] == "awaiting"
    assert job["awaiting"]["reason"] == "manual_pick"
    assert job["mode"] == "manual"
    payload = jobs._public(job)["awaiting"]["payload"]
    assert {p["n"] for p in payload["preselected"]} == {0, 1}
    # nada foi removido nem reenviado só por abrir a edição
    assert qbit.deleted == [] and len(qbit.added) == 2


def test_pedidos_de_download_validam(baixando):
    job, _ = baixando
    job["status"] = "awaiting"
    with pytest.raises(ValueError, match="durante o download"):
        asyncio.run(pipeline.resolve(job["id"], "edit_torrents", {}))
    job["status"] = "downloading"
    with pytest.raises(ValueError, match="não está no plano"):
        asyncio.run(pipeline.resolve(job["id"], "remove_torrent", {"torrent_n": 9}))
    with pytest.raises(ValueError, match="torrent_n"):
        asyncio.run(pipeline.resolve(job["id"], "remove_torrent", {}))


def test_lista_de_troca_mostra_o_torrent_em_uso_no_topo(baixando):
    """Sem o atual na lista, a tela não dizia qual era o torrent em uso."""
    job, _ = baixando
    lista = pipeline.torrent_alternatives(JOB, 1)
    assert lista[0]["title"] == DUB[0]
    assert [c["title"] for c in lista[1:]] == [DUB_NOVO[0]]
    # a troca automática segue sem ele (não troca um torrent por ele mesmo)
    assert DUB[0] not in [c["title"] for c in pipeline._alternatives_for(job, job["torrents"][1])]
