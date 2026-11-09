"""Progresso do download ponderado pelo TAMANHO real dos torrents.

Com um torrent de 25 GB e outro de 75 GB, o de 25 GB pronto é 25% do job —
não 50%. O tamanho só vale quando o qBittorrent já tem os metadados: antes
disso (magnet em metaDL) o que existe é o tamanho da busca, e o do Jackett
não é confiável. Torrent sem tamanho ocupa a fração dele (1/N das unidades).
"""
import pytest

from services.jobs import views

GB = 1024 ** 3


def _prog(pct, size=0, state="downloading"):
    return {"pct": pct, "speed": 0, "eta": None, "state": state, "seeds": 0,
            "name": "x", "size": size, "downloaded": int(size * pct / 100)}


def _filme(video, audio, kind="both"):
    return {"media_type": "movie", "kind": kind,
            "progress": {"video": video, "audio": audio}}


def test_filme_pondera_pelo_tamanho():
    job = _filme(_prog(100, 25 * GB, "uploading"), _prog(0, 75 * GB))
    assert views._download_pct(job) == pytest.approx(25.0)
    job = _filme(_prog(100, 25 * GB, "uploading"), _prog(50, 75 * GB))
    assert views._download_pct(job) == pytest.approx(62.5)


def test_filme_sem_metadados_ocupa_metade():
    """O áudio (25 GB) terminou e o vídeo ainda busca metadados: o vídeo
    ocupa 50%, mesmo com o tamanho do Jackett gravado pela troca de torrent."""
    job = _filme(_prog(0, 70 * GB, "metaDL"), _prog(100, 25 * GB, "uploading"))
    assert views._download_pct(job) == pytest.approx(50.0)
    job = _filme(_prog(0, 0, "forcedMetaDL"), _prog(40, 25 * GB))
    assert views._download_pct(job) == pytest.approx(20.0)


def test_filme_sem_nenhum_tamanho_e_media_simples():
    job = _filme(_prog(30, 0, "metaDL"), _prog(10, 0, "metaDL"))
    assert views._download_pct(job) == pytest.approx(20.0)


def test_filme_torrent_que_nao_reportou_conta_zero():
    """O denominador é o que o job PRECISA baixar: o vídeo sozinho não vira
    o progresso do job inteiro."""
    job = _filme(_prog(40, 10 * GB), None)
    assert views._download_pct(job) == pytest.approx(20.0)
    assert views._download_pct(_filme(None, None)) is None


def test_filme_so_um_papel():
    job = _filme(_prog(40, 10 * GB), None, kind="original")
    assert views._download_pct(job) == pytest.approx(40.0)


def _serie(*torrents):
    return {"media_type": "tv", "torrents": [
        {"n": i, "state": st, "coverage": [f"S01E{k:02d}" for k in range(1, eps + 1)],
         "progress": prog}
        for i, (st, eps, prog) in enumerate(torrents)]}


def test_serie_pondera_pelo_tamanho():
    job = _serie(("done", 10, _prog(100, 30 * GB, "stalledUP")),
                 ("downloading", 1, _prog(0, 10 * GB)))
    assert views._download_pct(job) == pytest.approx(75.0)


def test_serie_sem_metadados_pesa_pelos_episodios():
    """Pack de 10 episódios em 50% com 50 GB; avulso de 2 episódios ainda sem
    metadados: vale a média por episódio (5 GB) vezes 2."""
    job = _serie(("downloading", 10, _prog(50, 50 * GB)),
                 ("downloading", 2, _prog(0, 0, "metaDL")))
    assert views._download_pct(job) == pytest.approx(100 * 25 / 60)


def test_serie_sem_nenhum_tamanho_pesa_pelos_episodios():
    job = _serie(("downloading", 10, _prog(50, 0, "metaDL")),
                 ("downloading", 2, _prog(0, 0, "metaDL")))
    assert views._download_pct(job) == pytest.approx(100 * 5 / 12)


def test_serie_concluido_conta_cem():
    job = _serie(("done", 1, _prog(99.9, 10 * GB, "uploading")),
                 ("downloading", 1, _prog(0, 10 * GB)))
    assert views._download_pct(job) == pytest.approx(50.0)
    assert views._download_pct(_serie(("planned", 1, None))) is None
