"""Listagem do catálogo paginada no backend (scroll infinito da tela).

A busca, a ordenação e a paginação saíram do cliente: a tela só tem as
páginas que já rolou, então filtrar lá não acharia o resto. Aqui: pastas
temporárias reais, filtros + página, a ordenação igual à de antes e a
garantia de que só as pastas da página pagam a varredura de arquivos.
"""
import os
from pathlib import Path

import pytest

from services import catalog


def _movie(root: Path, folder: str, size: int = 1, extra: int = 0) -> Path:
    d = root / folder
    d.mkdir(parents=True)
    (d / "filme.mkv").write_bytes(b"x" * size)
    for i in range(extra):
        (d / f"extra{i}.srt").write_bytes(b"s")
    return d


def _series(root: Path, folder: str, eps: int) -> Path:
    d = root / folder / "Season 01"
    d.mkdir(parents=True)
    for e in range(1, eps + 1):
        (d / f"Serie S01E{e:02d}.mkv").write_bytes(b"x")
    return root / folder


@pytest.fixture
def colecao(temp_db, tmp_path):
    """Destino de filmes vazio (só ele: tira os semeados pelo init)."""
    for d in temp_db.list_destinations():
        temp_db.delete_destination(d["id"])
    root = tmp_path / "colecao"
    root.mkdir()
    dest = temp_db.add_destination("Filmes", str(root), True)
    return dest["id"], root


def _folders(r: dict) -> list[str]:
    return [i["folder"] for i in r["items"]]


def test_pagina_por_titulo_sem_sobreposicao(colecao):
    did, root = colecao
    for i in range(7):
        _movie(root, f"Filme Exemplo {i} (2000)")
    (root / "solto.txt").write_bytes(b"x")  # arquivo na raiz não é item

    p1 = catalog.list_items(did, page=1, per_page=3)
    assert p1["total"] == 7 and p1["total_all"] == 7
    assert p1["pages"] == 3 and p1["page"] == 1 and p1["per_page"] == 3
    p2 = catalog.list_items(did, page=2, per_page=3)
    p3 = catalog.list_items(did, page=3, per_page=3)
    assert len(p3["items"]) == 1  # sobra da última
    todas = _folders(p1) + _folders(p2) + _folders(p3)
    assert todas == [f"Filme Exemplo {i} (2000)" for i in range(7)]
    # além do fim: vazia, mas com a contagem certa (a tela para de pedir)
    p9 = catalog.list_items(did, page=9, per_page=3)
    assert p9["items"] == [] and p9["pages"] == 3


def test_sem_per_page_devolve_tudo(colecao):
    did, root = colecao
    for i in range(4):
        _movie(root, f"Filme {i}")
    r = catalog.list_items(did)
    assert len(r["items"]) == 4
    assert r["page"] == 1 and r["pages"] == 1 and r["total"] == 4
    item = r["items"][0]
    assert item["has_video"] is True and item["file_count"] == 1
    assert item["type"] == "movie" and item["size_human"] == "1 B"


def test_busca_acha_item_de_pagina_ainda_nao_carregada(colecao):
    """O caso que motivou mover a busca: com 1 página carregada, o filtro do
    cliente não via o item que só viria na última."""
    did, root = colecao
    for i in range(9):
        _movie(root, f"Aaa Filme {i} (2001)")
    _movie(root, "Zebra Exemplo (1990) [tmdbid-777]")

    sem_busca = catalog.list_items(did, page=1, per_page=3)
    assert "Zebra Exemplo (1990) [tmdbid-777]" not in _folders(sem_busca)

    r = catalog.list_items(did, q="  ZEBRA ", page=1, per_page=3)
    assert _folders(r) == ["Zebra Exemplo (1990) [tmdbid-777]"]
    assert r["total"] == 1 and r["total_all"] == 10 and r["pages"] == 1
    assert r["items"][0]["title"] == "Zebra Exemplo" and r["items"][0]["year"] == "1990"
    # o texto também casa com o nome da PASTA (o [tmdbid-N] não está no título)
    assert _folders(catalog.list_items(did, q="tmdbid-777")) == [
        "Zebra Exemplo (1990) [tmdbid-777]"]
    nada = catalog.list_items(did, q="inexistente", per_page=3)
    assert nada["items"] == [] and nada["total"] == 0 and nada["total_all"] == 10


def test_titulo_ignora_acento_e_caixa_e_inverte(colecao):
    did, root = colecao
    for f in ("eco (1999)", "Ébano (2001)", "Abelha (2002)", "Zinco (1980)"):
        _movie(root, f)
    asc = [i["title"] for i in catalog.list_items(did)["items"]]
    assert asc == ["Abelha", "Ébano", "eco", "Zinco"]
    desc = [i["title"] for i in catalog.list_items(did, order="desc")["items"]]
    assert desc == asc[::-1]


def test_ano_sem_ano_sempre_no_fim_e_empate_por_titulo(colecao):
    did, root = colecao
    for f in ("Beta (2010)", "Alfa (2010)", "Gama (1995)", "Sem Ano B", "Sem Ano A"):
        _movie(root, f)
    asc = _folders(catalog.list_items(did, sort="year"))
    assert asc == ["Gama (1995)", "Alfa (2010)", "Beta (2010)", "Sem Ano A", "Sem Ano B"]
    desc = _folders(catalog.list_items(did, sort="year", order="desc"))
    # inverte o ano, mas o empate continua por título crescente e os sem ano
    # continuam no fim (como o sort do cliente fazia)
    assert desc == ["Alfa (2010)", "Beta (2010)", "Gama (1995)", "Sem Ano A", "Sem Ano B"]


def test_tamanho_ordena_a_colecao_inteira_e_pagina(colecao):
    did, root = colecao
    _movie(root, "Medio", size=50)
    _movie(root, "Grande", size=900)
    _movie(root, "Pequeno", size=5)
    _movie(root, "Empate B", size=50)
    _series(root, "Serie Exemplo (2008)", eps=3)  # 3 bytes

    p1 = catalog.list_items(did, sort="size", order="desc", page=1, per_page=2)
    p2 = catalog.list_items(did, sort="size", order="desc", page=2, per_page=2)
    p3 = catalog.list_items(did, sort="size", order="desc", page=3, per_page=2)
    # empate de tamanho fica na ordem das pastas, nas duas direções
    assert _folders(p1) + _folders(p2) + _folders(p3) == [
        "Grande", "Empate B", "Medio", "Pequeno", "Serie Exemplo (2008)"]
    asc = _folders(catalog.list_items(did, sort="size"))
    assert asc == ["Serie Exemplo (2008)", "Pequeno", "Empate B", "Medio", "Grande"]
    serie = catalog.list_items(did, q="serie")["items"][0]
    assert serie["type"] == "series" and serie["file_count"] == 3


def test_so_as_pastas_da_pagina_sao_varridas(colecao, monkeypatch):
    did, root = colecao
    for i in range(6):
        _movie(root, f"Filme {i}", size=i + 1)
    varridas = []
    walk = catalog._walk_folder
    monkeypatch.setattr(catalog, "_walk_folder",
                        lambda p: varridas.append(p.name) or walk(p))

    catalog.list_items(did, page=2, per_page=2)
    assert varridas == ["Filme 2", "Filme 3"]  # título/ano: só a página

    varridas.clear()
    catalog.list_items(did, q="filme 5", per_page=2)
    assert varridas == ["Filme 5"]  # a busca filtra pelo nome, sem varrer

    # tamanho precisa de todas as que casam — mas uma vez só: as páginas
    # seguintes do mesmo scroll saem do cache
    varridas.clear()
    catalog.list_items(did, sort="size", page=1, per_page=2)
    # 2, 3 e 5 já estavam em cache das listagens acima
    assert sorted(varridas) == ["Filme 0", "Filme 1", "Filme 4"]
    varridas.clear()
    catalog.list_items(did, sort="size", page=2, per_page=2)
    catalog.list_items(did, sort="size", page=3, per_page=2)
    assert varridas == []


def test_cache_da_listagem_reflete_mudancas(colecao):
    did, root = colecao
    filme = _movie(root, "Filme Exemplo (2014)")
    serie = _series(root, "Serie Exemplo (2008)", eps=2)
    # mtime antigo fixo: o arquivo novo muda o mtime de forma garantida (sem
    # depender da granularidade do relógio do sistema de arquivos)
    os.utime(filme, ns=(0, 0))

    def contagem(folder):
        return next(i["file_count"] for i in catalog.list_items(did)["items"]
                    if i["folder"] == folder)

    assert contagem("Filme Exemplo (2014)") == 1
    assert contagem("Serie Exemplo (2008)") == 2

    # arquivo novo DIRETO na pasta do filme: o mtime da pasta denuncia na hora
    (filme / "legenda.srt").write_bytes(b"s")
    assert contagem("Filme Exemplo (2014)") == 2

    # episódio novo dentro de Season 01 não muda o mtime da pasta da série:
    # segue em cache até o TTL vencer...
    (serie / "Season 01" / "Serie S01E03.mkv").write_bytes(b"x")
    assert contagem("Serie Exemplo (2008)") == 2
    for k, (at, mtime, stats) in list(catalog._stats_cache.items()):
        catalog._stats_cache[k] = (at - catalog.LISTING_TTL_SECONDS - 1, mtime, stats)
    assert contagem("Serie Exemplo (2008)") == 3

    # ...ou até uma mudança feita pelo app: apagar pelo catálogo esquece a pasta
    catalog.delete_file(did, "Serie Exemplo (2008)", "Season 01/Serie S01E01.mkv")
    assert contagem("Serie Exemplo (2008)") == 2
    (serie / "Season 01" / "Serie S01E09.mkv").write_bytes(b"x")
    catalog.invalidate_library()  # job concluído, destino alterado...
    assert contagem("Serie Exemplo (2008)") == 3


def test_destino_inexistente_e_parametros_invalidos(temp_db, tmp_path):
    did = temp_db.add_destination("Fora", str(tmp_path / "desmontado"), False)["id"]
    r = catalog.list_items(did, per_page=10)
    assert r["exists"] is False and r["items"] == [] and r["total_all"] == 0
    with pytest.raises(catalog.CatalogError):
        catalog.list_items(did, sort="tamanho")
    with pytest.raises(catalog.CatalogError):
        catalog.list_items(did, order="cima")


def _client(monkeypatch):
    from fastapi.testclient import TestClient
    import main
    monkeypatch.setattr(main.auth, "is_password_set", lambda: True)
    monkeypatch.setattr(main.auth, "validate_token", lambda tok: True)
    return TestClient(main.app)


def test_api_pagina_e_valida(colecao, monkeypatch):
    did, root = colecao
    for i in range(5):
        _movie(root, f"Filme Exemplo {i} ({2000 + i})")
    c = _client(monkeypatch)
    h = {"Authorization": "Bearer x"}

    r = c.get(f"/api/catalog?destination_id={did}&sort=year&order=desc"
              "&page=2&per_page=2", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert [i["year"] for i in body["items"]] == ["2002", "2001"]
    assert body["pages"] == 3 and body["total"] == 5 and body["total_all"] == 5
    assert "disk" in body["destination"]

    r = c.get(f"/api/catalog?destination_id={did}&q=exemplo 3", headers=h)
    assert [i["folder"] for i in r.json()["items"]] == ["Filme Exemplo 3 (2003)"]

    assert c.get(f"/api/catalog?destination_id={did}&per_page=0", headers=h).status_code == 400
    assert c.get(f"/api/catalog?destination_id={did}&sort=x", headers=h).status_code == 400
