"""Fila de conversão (lock global): quem espera a vez mostra que está na fila.

Caso real: dois jobs de série convertendo um episódio por vez. Um terminava
o episódio, soltava o lock e já pedia de novo para o próximo — mas o lock
tinha sido REPASSADO para o outro job, que ainda não tinha rodado:
`locked()` dizia livre, o aviso de fila não saía e o job ficava esperando
com "Gravando o arquivo final..." e "Convertendo" na tela.
"""
import asyncio

import pytest

from services.jobs import runtime


@pytest.fixture(autouse=True)
def lock_novo(monkeypatch):
    monkeypatch.setattr(runtime, "_merge_lock", None)


def _job():
    return {"detail": "", "progress": {"merge": None}}


def test_quem_espera_no_repasse_mostra_a_fila():
    a, b = _job(), _job()
    visto = {}

    async def main():
        a_terminou, b_pode_sair = asyncio.Event(), asyncio.Event()

        async def job_a():
            async with runtime._merge_slot(a, "a: na fila", set_status=False):
                a["detail"] = "Gravando o arquivo final..."
                a["progress"]["merge"] = {"phase": runtime.PHASE_MUX}
                await a_terminou.wait()
            a["progress"]["merge"] = None
            # próximo episódio, logo depois de soltar (como o laço de série)
            async with runtime._merge_slot(a, "a: na fila", set_status=False):
                visto["a_entrou"] = dict(a["progress"])

        async def job_b():
            async with runtime._merge_slot(b, "b: na fila", set_status=False):
                visto["b_entrou"] = dict(b["progress"])
                await b_pode_sair.wait()

        ta = asyncio.create_task(job_a())
        await asyncio.sleep(0)
        tb = asyncio.create_task(job_b())
        await asyncio.sleep(0)
        # b esperando atrás de a: já mostra a fila
        assert b["detail"] == "b: na fila"
        assert b["progress"]["merge"] == {"phase": runtime.PHASE_QUEUE}

        a_terminou.set()
        for _ in range(5):
            await asyncio.sleep(0)
        # o lock foi para b; a pediu de novo e espera — mostra a fila, não a
        # etapa anterior
        assert visto["b_entrou"]["merge"] is None
        assert a["detail"] == "a: na fila"
        assert a["progress"]["merge"] == {"phase": runtime.PHASE_QUEUE}

        b_pode_sair.set()
        await asyncio.gather(ta, tb)
        # ao entrar, a etapa da fila sai (quem roda carimba a sua)
        assert visto["a_entrou"]["merge"] is None

    asyncio.run(main())


def test_sem_espera_nao_mexe_no_estado(monkeypatch):
    chamadas = []
    monkeypatch.setattr(runtime, "_set", lambda *a: chamadas.append(a))
    job = _job()
    job["detail"] = "Baixado"

    async def main():
        async with runtime._merge_slot(job):
            pass

    asyncio.run(main())
    assert chamadas == [] and job["detail"] == "Baixado"
    assert job["progress"]["merge"] is None


def test_filme_na_fila_vai_para_merging(monkeypatch):
    chamadas = []
    monkeypatch.setattr(runtime, "_set",
                        lambda j, st, d: (chamadas.append((st, d)),
                                          j.update(status=st, detail=d)))
    job = _job()

    async def main():
        lock = runtime._get_merge_lock()
        await lock.acquire()
        t = asyncio.create_task(_entra(job))
        await asyncio.sleep(0)
        assert chamadas == [("merging", "Na fila de conversão...")]
        assert job["progress"]["merge"] == {"phase": runtime.PHASE_QUEUE}
        lock.release()
        await t

    async def _entra(j):
        async with runtime._merge_slot(j):
            pass

    asyncio.run(main())
    assert job["progress"]["merge"] is None
