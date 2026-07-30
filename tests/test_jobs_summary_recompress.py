"""summary(): o dropdown de processos mostra conversões, inclusive recompressões.

Elas eram excluídas do summary inteiro (para não bagunçar a tela de Filmes, que
chaveia por tmdb_id) e por isso sumiam também do dropdown. Agora vêm marcadas.
"""
from services import jobs


def _job(jid, status, mode=None, tmdb_id=7, merge_pct=None):
    j = {
        "id": jid, "tmdb_id": tmdb_id, "language": "pt", "kind": "both",
        "status": status, "created_at": "2026-07-29T21:00:00",
        "detail": "", "progress": {}, "convert": None,
        "movie": {"original_title": "Filme", "year": "1968"},
    }
    if mode:
        j["mode"] = mode
    if merge_pct is not None:
        j["progress"]["merge"] = {"pct": merge_pct}
    jobs._jobs[jid] = j
    return j


def test_recompressao_convertendo_aparece(temp_db):
    _job("r1", "merging", mode="recompress", merge_pct=42.0)
    s = jobs.summary()
    assert len(s) == 1
    assert s[0]["id"] == "r1"
    assert s[0]["state"] == "converting"
    assert s[0]["pct"] == 42.0          # a barra do dropdown precisa do %
    assert s[0]["recompress"] is True   # ...e a tela de Filmes precisa ignorar


def test_merge_normal_aparece_sem_a_flag(temp_db):
    _job("m1", "merging", merge_pct=10.0)
    s = jobs.summary()
    assert s[0]["state"] == "converting"
    assert s[0]["recompress"] is False


def test_concluido_e_cancelado_ficam_fora(temp_db):
    _job("d1", "done")
    _job("c1", "cancelled")
    assert jobs.summary() == []


def test_conversoes_vem_antes_de_downloads(temp_db):
    _job("dl", "downloading", tmdb_id=1)
    _job("cv", "merging", mode="recompress", tmdb_id=2, merge_pct=5.0)
    assert [x["id"] for x in jobs.summary()] == ["cv", "dl"]
