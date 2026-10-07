"""Tradução do caminho que o qBittorrent reporta para o caminho local.

Caso real: o usuário troca a pasta de salvamento NO PRÓPRIO qBittorrent quando
falta espaço. O caminho reportado passa a ter o prefixo de OUTRO destino de
torrents, e a tradução só pelo par do job dava "caminho não existe". Agora
valem todos os destinos cadastrados, e ganha o primeiro que existe em disco.
"""
import pytest

import config
from services.jobs import downloads, runtime


def _job(save="", local=""):
    return {"id": "j1", "torrent_save_path": save, "torrent_local_path": local}


def test_par_do_job_continua_valendo(temp_db, tmp_path):
    local = tmp_path / "dl"
    (local / "Filme").mkdir(parents=True)
    p = runtime._map_qbit_path(_job("/downloads", str(local)), "/downloads/Filme")
    assert p == local / "Filme"


def test_pasta_trocada_no_qbittorrent_cai_em_outro_destino(temp_db, tmp_path):
    """O job foi criado com o destino A; o torrent foi movido no qBittorrent
    para a pasta do destino B. O caminho reportado tem o prefixo de B, que
    está cadastrado — e existe nesta máquina."""
    a, b = tmp_path / "a", tmp_path / "b"
    (b / "Filme").mkdir(parents=True)
    temp_db.add_torrent_target("A", "/downloads", str(a), is_default=True)
    temp_db.add_torrent_target("B (disco maior)", "/mnt/big", str(b), is_default=False)
    job = _job("/downloads", str(a))
    assert runtime._map_qbit_path(job, "/mnt/big/Filme") == b / "Filme"


def test_sem_destino_cadastrado_da_erro_listando_o_que_tentou(temp_db, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "QBIT_PATH_MAP", [])
    temp_db.add_torrent_target("A", "/downloads", str(tmp_path / "a"), is_default=True)
    job = _job("/downloads", str(tmp_path / "a"))
    with pytest.raises(RuntimeError) as e:
        downloads._find_video_file(job, "/mnt/outro/Filme")
    msg = str(e.value)
    assert "/mnt/outro/Filme" in msg and "tentados" in msg
    assert "cadastre em Configurações" in msg


def test_prefere_o_candidato_que_existe(temp_db, tmp_path):
    """Dois destinos com o MESMO save_path (um cadastro antigo apontando para
    uma pasta que já não está montada): vale o que existe, não o primeiro."""
    velho, novo = tmp_path / "velho", tmp_path / "novo"
    (novo / "Filme").mkdir(parents=True)
    temp_db.add_torrent_target("antigo", "/downloads", str(velho), is_default=True)
    temp_db.add_torrent_target("atual", "/downloads", str(novo), is_default=False)
    p = runtime._map_qbit_path(_job(), "/downloads/Filme")
    assert p == novo / "Filme"


def test_env_e_caminho_cru_continuam_como_fallback(temp_db, tmp_path, monkeypatch):
    local = tmp_path / "env"
    (local / "Filme").mkdir(parents=True)
    monkeypatch.setattr(config, "QBIT_PATH_MAP", [("/qb", str(local))])
    assert runtime._map_qbit_path(_job(), "/qb/Filme") == local / "Filme"
    # qBittorrent na mesma máquina: o caminho reportado já é o local
    direto = tmp_path / "direto" / "Filme"
    direto.mkdir(parents=True)
    assert runtime._map_qbit_path(_job(), str(direto)) == direto
