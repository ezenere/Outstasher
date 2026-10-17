"""Persistência do documento do job: _event não regrava o job a cada evento.

O evento (linha em `events`) grava sempre na hora; o documento do job grava
no máximo uma vez por config.EVENT_PERSIST_SECONDS (o resto fica "sujo" até o
flush). _set grava na hora; job removido não volta por flush atrasado.
"""
import asyncio
import uuid

import pytest

import config
from services.jobs import runtime


def _job(status="downloading") -> dict:
    return {"id": uuid.uuid4().hex[:12], "created_at": "2026-01-01T00:00:00",
            "status": status, "detail": "", "title": "Serie Exemplo",
            "episodes": {}}


class _GravacoesDoJob:
    """Gravações da linha do job na tabela `jobs`, por id, observadas no SQL
    que o SQLite executa DE VERDADE (trace da conexão).

    Não substitui função nenhuma do store: interceptar `store.upsert_job` só
    enxergava quem chama pelo pacote, e as chamadas internas entre os módulos
    do store passavam por fora. No trace toda gravação aparece, venha de onde
    vier."""

    def __init__(self):
        self.stmts: list[str] = []

    def __call__(self, sql: str):
        w = sql.lstrip().split(None, 3)
        if len(w) >= 3 and ((w[0].upper() == "INSERT" and w[2] == "jobs")
                            or (w[0].upper() == "UPDATE" and w[1] == "jobs")):
            self.stmts.append(sql)

    def __getitem__(self, job_id: str) -> int:
        return sum(1 for s in self.stmts if f"'{job_id}'" in s)


@pytest.fixture
def writes(temp_db, monkeypatch):
    """Conta as gravações do DOCUMENTO do job, por id (ver _GravacoesDoJob)."""
    contador = _GravacoesDoJob()
    temp_db._conn.set_trace_callback(contador)
    # janela longa: o teste controla a passagem do tempo à mão
    monkeypatch.setattr(config, "EVENT_PERSIST_SECONDS", 60.0)
    yield contador
    if temp_db._conn is not None:
        temp_db._conn.set_trace_callback(None)


def _janela_passou(job):
    runtime._last_persist[job["id"]] -= config.EVENT_PERSIST_SECONDS + 1


def test_rajada_de_eventos_grava_o_job_no_maximo_uma_vez(temp_db, writes):
    job = _job()
    for i in range(200):
        job["detail"] = f"linha {i}"
        runtime._event(job, "merge", f"linha {i}")
    # o primeiro evento cria a linha; os outros 199 só marcam sujo
    assert writes[job["id"]] == 1
    assert job["id"] in runtime._dirty
    # nenhum evento se perde: só o documento é que espera
    assert len(temp_db.load_events(job["id"])) == 200
    assert temp_db.get_job(job["id"])["detail"] == "linha 0"
    # flush antes da janela fechar não grava de novo
    assert runtime.flush_dirty() == 0
    assert writes[job["id"]] == 1


def test_primeiro_evento_depois_da_janela_grava_na_hora(temp_db, writes):
    job = _job()
    runtime._event(job, "info", "criado")
    _janela_passou(job)
    job["detail"] = "depois do silêncio"
    runtime._event(job, "qbit", "torrent adicionado")
    assert writes[job["id"]] == 2
    assert job["id"] not in runtime._dirty
    assert temp_db.get_job(job["id"])["detail"] == "depois do silêncio"


def test_set_grava_na_hora_mesmo_dentro_da_janela(temp_db, writes):
    job = _job()
    runtime._event(job, "info", "criado")
    job["episodes"]["S01E01"] = {"state": "done"}
    runtime._event(job, "merge", "episódio pronto")  # sujo, não gravou
    assert writes[job["id"]] == 1
    runtime._set(job, "awaiting", "esperando o usuário")
    assert writes[job["id"]] == 2
    assert job["id"] not in runtime._dirty
    salvo = temp_db.get_job(job["id"])
    assert salvo["status"] == "awaiting"
    assert salvo["detail"] == "esperando o usuário"
    # a mutação feita antes do _set (que só estava suja) foi junto
    assert salvo["episodes"]["S01E01"]["state"] == "done"
    assert [e["kind"] for e in temp_db.load_events(job["id"])] == [
        "info", "merge", "status"]


def test_flush_grava_os_sujos_quando_a_janela_fecha(temp_db, writes):
    job = _job()
    runtime._event(job, "info", "criado")
    job["episodes"]["S01E02"] = {"state": "failed"}
    runtime._event(job, "merge", "falhou")
    assert "S01E02" not in temp_db.get_job(job["id"])["episodes"]
    _janela_passou(job)
    assert runtime.flush_dirty() == 1
    assert temp_db.get_job(job["id"])["episodes"]["S01E02"]["state"] == "failed"
    assert job["id"] not in runtime._dirty
    # já limpo: o próximo flush não regrava
    assert runtime.flush_dirty(force=True) == 0
    assert writes[job["id"]] == 2


def test_flush_forcado_grava_tudo_no_desligamento(temp_db, writes):
    a, b = _job(), _job()
    for j in (a, b):
        runtime._event(j, "info", "criado")
        j["detail"] = "final"
        runtime._event(j, "merge", "fim")
    assert runtime.flush_dirty(force=True) == 2
    assert temp_db.get_job(a["id"])["detail"] == "final"
    assert temp_db.get_job(b["id"])["detail"] == "final"


def test_job_removido_nao_volta(temp_db, writes):
    job = _job("cancelled")
    runtime._event(job, "info", "criado")
    job["detail"] = "mutação pendente"
    runtime._event(job, "merge", "linha")  # sujo
    runtime._delete_job(job["id"])
    assert temp_db.get_job(job["id"]) is None
    # nem o flush atrasado, nem um evento/_set tardio da task que morria
    assert runtime.flush_dirty(force=True) == 0
    runtime._event(job, "info", "evento tardio")
    runtime._set(job, "error", "erro tardio")
    assert temp_db.get_job(job["id"]) is None
    assert temp_db.load_events(job["id"]) == []


def test_flush_atrasado_nao_recria_job_apagado_por_fora(temp_db, writes):
    # apagado direto no banco (sem passar por _delete_job): o flush só
    # atualiza linha existente, então também não ressuscita
    job = _job()
    runtime._event(job, "info", "criado")
    runtime._event(job, "merge", "linha")  # sujo
    temp_db.delete_job(job["id"])
    runtime.flush_dirty(force=True)
    assert temp_db.get_job(job["id"]) is None


def test_persist_loop_grava_os_sujos(temp_db, writes, monkeypatch):
    monkeypatch.setattr(config, "EVENT_PERSIST_SECONDS", 0.05)
    job = _job()
    runtime._event(job, "info", "criado")
    job["detail"] = "do loop"
    runtime._event(job, "merge", "linha")
    assert job["id"] in runtime._dirty

    async def _roda():
        task = asyncio.create_task(runtime.persist_loop())
        await asyncio.sleep(0.3)
        task.cancel()

    asyncio.run(_roda())
    assert job["id"] not in runtime._dirty
    assert temp_db.get_job(job["id"])["detail"] == "do loop"


def test_desligamento_do_servidor_grava_os_sujos(temp_db, writes):
    import main
    job = _job("done")  # terminal: o load() do lifespan não o retoma
    runtime._event(job, "info", "criado")
    job["detail"] = "limpeza pós-entrega"
    runtime._event(job, "qbit", "torrents removidos")
    assert temp_db.get_job(job["id"])["detail"] == ""

    async def _sobe_e_desce():
        async with main.lifespan(main.app):
            pass

    asyncio.run(_sobe_e_desce())
    assert temp_db.get_job(job["id"])["detail"] == "limpeza pós-entrega"
