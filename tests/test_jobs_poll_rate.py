"""Ritmo do watchdog do qBittorrent: rápido só enquanto a UI acompanha.

Sem ninguém lendo o progresso, o valor em memória não serve a ninguém — o
watchdog cai para o intervalo ocioso e poupa requests ao qBittorrent.
"""
import time

import config
from services import jobs


def _reset():
    jobs._last_progress_demand = None  # None = ninguém pediu ainda


def test_ocioso_por_padrao():
    _reset()
    assert jobs.progress_demanded() is False
    assert jobs.poll_interval() == config.POLL_IDLE_INTERVAL_SECONDS


def test_touch_acelera():
    _reset()
    jobs.touch_progress_demand()
    assert jobs.progress_demanded() is True
    assert jobs.poll_interval() == config.POLL_INTERVAL_SECONDS


def test_janela_expira(monkeypatch):
    """Passada a janela sem ninguém pedir progresso, volta ao ritmo ocioso."""
    _reset()
    jobs.touch_progress_demand()
    assert jobs.poll_interval() == config.POLL_INTERVAL_SECONDS

    # avança o relógio para além da janela (sem sleep de verdade)
    real = time.monotonic()
    monkeypatch.setattr(jobs.time, "monotonic",
                        lambda: real + config.POLL_ACTIVE_WINDOW_SECONDS + 1)
    assert jobs.progress_demanded() is False
    assert jobs.poll_interval() == config.POLL_IDLE_INTERVAL_SECONDS


def test_janela_renova(monkeypatch):
    """Uma nova consulta dentro da janela mantém o ritmo rápido."""
    _reset()
    real = time.monotonic()
    jobs.touch_progress_demand()

    # quase no fim da janela, a UI pede de novo
    t = real + config.POLL_ACTIVE_WINDOW_SECONDS - 1
    monkeypatch.setattr(jobs.time, "monotonic", lambda: t)
    jobs.touch_progress_demand()

    # o que seria o fim da janela original já passou, mas o touch a renovou
    t2 = real + config.POLL_ACTIVE_WINDOW_SECONDS + 1
    monkeypatch.setattr(jobs.time, "monotonic", lambda: t2)
    assert jobs.progress_demanded() is True
    assert jobs.poll_interval() == config.POLL_INTERVAL_SECONDS


def test_intervalo_ocioso_maior_que_o_rapido():
    """Sanidade dos valores: ocioso mais lento, e a janela cobre o tick da UI."""
    assert config.POLL_IDLE_INTERVAL_SECONDS > config.POLL_INTERVAL_SECONDS
    # a lista de jobs recarrega a cada 15s, mas o detalhe a cada 1s e a
    # janela precisa cobrir com folga o tick de quem está olhando
    assert config.POLL_ACTIVE_WINDOW_SECONDS >= 5
