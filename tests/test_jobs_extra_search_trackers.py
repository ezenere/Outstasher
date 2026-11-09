"""Buscas extras não consultam tracker que saiu do Jackett.

Caso real: um tracker removido do Jackett continuava marcado nas regras de
busca extra, e cada job o consultava — erro/timeout a cada busca. Agora a
busca filtra as regras contra a lista de indexers configurados (com cache
curto), pula o ausente e deixa UM aviso legível no job. Se a lista não vier,
nada é descartado: sem ela não dá para saber o que sumiu.

O Jackett é simulado no transporte HTTP (httpx.MockTransport): a listagem
Torznab (t=indexers) e a busca por indexer passam pelo código de verdade de
services/jackett.py.
"""
import asyncio
import re

import httpx
import pytest

from services import jackett, jobs, store
from services.jobs import search

AUSENTE = "idx-sumiu"
AVISO = (f"⚠️ Tracker {AUSENTE} está marcado nas buscas 'Sem o ano' e "
         f"'Trocando romanos e sem o ano' (Português) e não está no Jackett — ignorado")


class JackettFalso:
    """Jackett no transporte: lista `configured` em t=indexers e responde às
    buscas; indexer fora da lista dá 404 (o que o Jackett de verdade faz)."""

    def __init__(self, configured=("idx-bom", "idx-outro")):
        self.configured = list(configured)
        self.listing_error = False
        self.listings = 0
        self.searched: list[tuple[str, str]] = []  # (indexer, query)

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/torznab/api"):
            self.listings += 1
            if self.listing_error:
                raise httpx.ConnectError("conexão recusada", request=request)
            body = "".join(
                f'<indexer id="{i}" configured="true"><title>{i}</title>'
                f"<language>pt-BR</language></indexer>" for i in self.configured)
            return httpx.Response(200, text=f"<indexers>{body}</indexers>")
        m = re.fullmatch(r"/api/v2\.0/indexers/([^/]+)/results", path)
        assert m, path
        indexer, query = m.group(1), request.url.params["Query"]
        self.searched.append((indexer, query))
        if indexer != "all" and indexer not in self.configured:
            return httpx.Response(404, text="Indexer não configurado")
        tracker = "t-all" if indexer == "all" else indexer
        return httpx.Response(200, json={"Results": [
            _item("Filme Exemplo II 2014 1080p WEB-DL", tracker),
            _item("Filme Teste & Cia II 2014 1080p Dublado", tracker),
        ]})

    def extra_indexers(self) -> list[str]:
        return [i for i, _ in self.searched if i != "all"]


def _item(title, tracker):
    return {"Title": title, "Seeders": 10, "Size": 2000,
            "MagnetUri": f"magnet:?xt=urn:btih:{abs(hash(title)) % 10 ** 12:040x}",
            "Link": None, "Tracker": tracker, "TrackerId": tracker}


@pytest.fixture
def jackett_falso(temp_db, monkeypatch):
    fake = JackettFalso()
    real_client = httpx.AsyncClient

    def client(**kw):
        return real_client(transport=httpx.MockTransport(fake.handler), **kw)

    monkeypatch.setattr(jackett.httpx, "AsyncClient", client)
    # cache da lista é estado de módulo: cada teste começa (e termina) sem ele
    monkeypatch.setattr(jackett, "_ids_cache", None)

    async def fake_details(tmdb_id, lang):
        return {"id": tmdb_id, "original_title": "Filme Exemplo II",
                "localized_title": "Filme Teste & Cia II", "english_title": None,
                "original_language": "en", "year": "2014", "overview": None,
                "poster": None}
    monkeypatch.setattr(search.tmdb, "details", fake_details)
    # o ausente marcado em duas regras; o título tem 3 grafias ("&", "e",
    # sem o "&"), então cada regra vira 3 buscas nele — e o aviso tem de ser um só
    store.set_extra_search_rules({"pt": {
        "no_year": ["idx-bom", AUSENTE],
        "roman_no_year": [AUSENTE, "idx-outro"],
    }})
    return fake


def _run_search(jid="ex1") -> dict:
    job = {"id": jid, "tmdb_id": 7, "language": "pt", "mode": "manual",
           "kind": "both", "status": "searching", "detail": "", "output": None,
           "movie": None, "created_at": "2026-01-01T00:00:00",
           "progress": {"video": None, "audio": None},
           "search_queries": [], "search_candidates": []}
    jobs._jobs[jid] = job
    asyncio.run(search._search(job))
    return job


def _messages(job) -> list[str]:
    return [e["message"] for e in store.load_events(job["id"])]


def test_tracker_fora_do_jackett_nao_e_consultado_e_avisa_uma_vez(jackett_falso):
    job = _run_search()
    extras = jackett_falso.extra_indexers()
    assert AUSENTE not in extras
    # os bons rodam em todas as grafias de cada regra
    assert extras.count("idx-bom") == 3 and extras.count("idx-outro") == 3
    msgs = _messages(job)
    assert [m for m in msgs if AUSENTE in m] == [AVISO]
    # a contagem anunciada é a das buscas que de fato rodam
    assert "6 busca(s) extra(s) configurada(s) para Português — rodando em paralelo" in msgs
    assert not any("falhou" in m for m in msgs)


def test_listagem_do_jackett_falhando_nao_descarta_nada(jackett_falso):
    jackett_falso.listing_error = True
    job = _run_search()
    # sem a lista não dá para saber o que sumiu: tenta todos, como antes
    extras = jackett_falso.extra_indexers()
    assert extras.count(AUSENTE) == 6
    msgs = _messages(job)
    assert not any("não está no Jackett" in m for m in msgs)
    # o ausente falha do jeito antigo (evento de erro por busca), o job segue
    assert sum(1 for m in msgs if f"@ {AUSENTE}]" in m and "falhou" in m) == 6
    assert search.candidates.selectable(job, "audio")


def test_lista_vazia_nao_descarta_nada(jackett_falso):
    # Jackett respondendo sem indexer nenhum é suspeito (recém-subido, config
    # perdida): não pode tirar das buscas os trackers bons
    jackett_falso.configured = []
    asyncio.run(jackett.list_indexers())  # a tela também vê a lista vazia
    assert asyncio.run(jackett.configured_indexer_ids()) is None


def test_lista_em_cache_entre_jobs_e_renovada_pela_tela(jackett_falso):
    _run_search("ex1")
    _run_search("ex2")
    assert jackett_falso.listings == 1  # o lote inteiro custa uma listagem

    # o tracker volta ao Jackett; a tela de buscas extras recarrega a lista
    # (GET /api/jackett/indexers) e o próximo job já busca nele, sem esperar o TTL
    jackett_falso.configured.append(AUSENTE)
    asyncio.run(jackett.list_indexers())
    jackett_falso.searched.clear()
    job = _run_search("ex3")
    assert jackett_falso.listings == 2
    assert jackett_falso.extra_indexers().count(AUSENTE) == 6
    assert not any("não está no Jackett" in m for m in _messages(job))


def test_cache_expira_e_falha_fica_guardada_pouco(jackett_falso):
    assert asyncio.run(jackett.configured_indexer_ids()) == {"idx-bom", "idx-outro"}
    ts, ids = jackett._ids_cache
    # dentro do TTL não lista de novo; vencido, lista
    jackett._ids_cache = (ts - jackett.IDS_TTL_S + 5, ids)
    asyncio.run(jackett.configured_indexer_ids())
    assert jackett_falso.listings == 1
    jackett._ids_cache = (ts - jackett.IDS_TTL_S - 1, ids)
    jackett_falso.listing_error = True
    assert asyncio.run(jackett.configured_indexer_ids()) is None
    assert jackett_falso.listings == 2
    # a falha vale só IDS_FAIL_TTL_S: o lote não espera um timeout por job...
    assert asyncio.run(jackett.configured_indexer_ids()) is None
    assert jackett_falso.listings == 2
    # ...e passada a janela, volta a perguntar (e a filtrar)
    ts, _ = jackett._ids_cache
    jackett._ids_cache = (ts - jackett.IDS_FAIL_TTL_S - 1, None)
    jackett_falso.listing_error = False
    assert asyncio.run(jackett.configured_indexer_ids()) == {"idx-bom", "idx-outro"}
    assert jackett_falso.listings == 3


def test_jobs_juntos_esperam_a_mesma_listagem(jackett_falso):
    async def lote():
        return await asyncio.gather(*(jackett.configured_indexer_ids() for _ in range(5)))
    assert all(r == {"idx-bom", "idx-outro"} for r in asyncio.run(lote()))
    assert jackett_falso.listings == 1
