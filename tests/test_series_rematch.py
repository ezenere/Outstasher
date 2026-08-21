"""Rematch de episódios desalinhados (todos × todos).

Placar por fingerprint sintético (sem ffmpeg), atribuição gulosa, aplicação
de pares e ignorados no job, o gate no fim do laço de merge e o cache de
fingerprint em disco.
"""
import asyncio
from pathlib import Path

import numpy as np
import pytest

from services.series import merge_runner, rematch
from services.series.align import fingerprint


def _job(n_eps: int = 3) -> dict:
    eps = {}
    for i in range(1, n_eps + 1):
        k = f"S01E{i:02d}"
        eps[k] = {
            "season": 1, "episode": i, "name": f"Episodio {i}",
            "air_date": "2020-01-01", "runtime": 40,
            "state": "failed",
            "error": "conflito de alinhamento: quase nada dos dois arquivos casa",
            "src": {"original": f"/x/orig_{k}.mkv", "dubbed": f"/x/dub_{k}.mkv"},
            "output": None,
        }
    return {
        "id": "rematch01", "media_type": "tv", "language": "pt",
        "status": "merging", "detail": "", "episodes": eps,
        "torrents": [], "awaiting": None, "report": None,
        "progress": {}, "output": None, "mode": "files",
        "created_at": "2026-08-20T00:00:00",
    }


def _hashes(rng, n=1200):
    return rng.integers(0, 2 ** 63, size=n, dtype=np.uint64)


# -------------------- atribuição --------------------

def test_atribuicao_gulosa_respeita_piso_e_unicidade():
    scores = [
        (0.92, 0.0, "S01E01", "S01E02"),
        (0.90, 0.0, "S01E02", "S01E01"),
        (0.88, 0.0, "S01E01", "S01E03"),   # E01 já casou: ignora
        (0.20, 0.0, "S01E03", "S01E03"),   # abaixo do piso: fora
    ]
    pairs = rematch.assign(scores, min_frac=0.30)
    assert pairs == {"S01E01": ("S01E02", 0.92), "S01E02": ("S01E01", 0.90)}


# -------------------- placar todos × todos --------------------

def test_placar_encontra_ordem_trocada(monkeypatch):
    """Dois episódios com os dublados TROCADOS entre si: o placar cruzado tem
    que apontar dub(E01)↔orig(E02) e dub(E02)↔orig(E01), com as combinações
    originais (que falharam) lá embaixo."""
    rng = np.random.default_rng(7)
    c1, c2 = _hashes(rng), _hashes(rng)
    job = _job(2)
    conteudo = {
        "/x/orig_S01E01.mkv": c1, "/x/dub_S01E01.mkv": c2,   # dub trocado
        "/x/orig_S01E02.mkv": c2, "/x/dub_S01E02.mkv": c1,
    }
    monkeypatch.setattr(fingerprint, "crop_params", lambda p, d=None: None)
    monkeypatch.setattr(fingerprint, "dhash_cached",
                        lambda p, crop=None, **kw: conteudo[p])
    scores = rematch.score_all(rematch.mismatched(job), log=lambda m: None)
    pairs = rematch.assign(scores)
    assert {k: v[0] for k, v in pairs.items()} == {
        "S01E01": "S01E02", "S01E02": "S01E01"}
    assert all(frac > 0.9 for _, frac in pairs.values())
    proprio = [s for s in scores if s[2] == s[3]]
    assert all(s[0] < rematch.MIN_FRACTION for s in proprio), proprio


# -------------------- aplicação no job --------------------

def test_aplicar_pares_religa_dublado_e_reenfileira(temp_db):
    job = _job(3)
    rematch.apply_pairs(job, {"S01E01": "S01E02", "S01E02": "S01E01"},
                        fracs={"S01E01": 0.95, "S01E02": 0.94})
    e1, e2, e3 = (job["episodes"][k] for k in ("S01E01", "S01E02", "S01E03"))
    assert e1["src"]["dubbed"] == "/x/dub_S01E02.mkv"
    assert e2["src"]["dubbed"] == "/x/dub_S01E01.mkv"
    assert e1["state"] == e2["state"] == "downloaded"
    assert e1["error"] is None
    assert e3["state"] == "failed"          # sem par: continua no pool

    with pytest.raises(ValueError):
        rematch.apply_pairs(job, {"S01E03": "S01E01"})  # dub já consumido? não:
        # E01 saiu do pool (downloaded) — par com quem não está no pool é erro


def test_ignorar_vira_pulo_decidido(temp_db):
    job = _job(2)
    rematch.apply_ignores(job, ["S01E02", "S09E99"])   # inexistente: ignora
    assert job["episodes"]["S01E02"]["state"] == "skipped_mismatch"
    assert job["episodes"]["S01E02"]["error"] is None
    assert job["episodes"]["S01E01"]["state"] == "failed"


# -------------------- gate no fim do laço --------------------

def test_fim_do_merge_abre_gate_com_2_desalinhados(temp_db):
    job = _job(2)
    asyncio.run(merge_runner.merge_all(job))
    assert job["status"] == "awaiting"
    gate = job["awaiting"]
    assert gate["reason"] == "mismatched_pairs"
    assert sorted(gate["payload"]["mismatched"]) == ["S01E01", "S01E02"]
    assert gate["payload"]["mismatched"]["S01E01"]["dubbed"] == "dub_S01E01.mkv"


def test_um_desalinhado_so_nao_abre_gate(temp_db):
    job = _job(1)
    asyncio.run(merge_runner.merge_all(job))
    assert job["status"] == "error"          # 0/1 < 75%: relatório fecha o job
    assert job["awaiting"] is None


def test_finalizar_processamento_fecha_sem_gate(temp_db):
    job = _job(2)
    asyncio.run(rematch.finish_now(job))
    assert job["status"] == "error"          # 0/2 concluído: < 75%
    assert job["report"]["failed"] == ["S01E01", "S01E02"]
    # e um novo fim de laço NÃO reabre o gate
    job["status"] = "merging"
    asyncio.run(merge_runner.merge_all(job))
    assert job["awaiting"] is None


def test_ignorados_ficam_fora_da_conta_dos_75(temp_db):
    job = _job(3)
    job["episodes"]["S01E01"]["state"] = "done"
    job["episodes"]["S01E01"]["error"] = None
    rematch.apply_ignores(job, ["S01E02", "S01E03"])
    asyncio.run(merge_runner.merge_all(job))
    assert job["status"] == "done"           # 1/1 tentado; ignorados fora
    assert job["report"]["skipped"] == ["S01E02", "S01E03"]


# -------------------- cache de fingerprint --------------------

@pytest.mark.ffmpeg
def test_dhash_cached_persiste_e_reusa(tmp_path, monkeypatch):
    import subprocess

    import config
    monkeypatch.setattr(config, "DB_DIR", tmp_path)
    video = tmp_path / "ep.mkv"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc2=duration=6:size=192x108:rate=10",
         "-c:v", "libx264", "-preset", "ultrafast", str(video)], check=True)
    h1 = fingerprint.dhash_cached(str(video))
    salvos = list((tmp_path / "fpcache").glob("*.npy"))
    assert len(salvos) == 1 and len(h1) > 0
    # segunda chamada NÃO recalcula: se chamar o dhash_stream, o teste falha
    monkeypatch.setattr(fingerprint, "dhash_stream",
                        lambda *a, **k: pytest.fail("cache não foi usado"))
    h2 = fingerprint.dhash_cached(str(video))
    assert np.array_equal(h1, h2)
