"""Modo --series do merge.py: detecção de SxxExx e merge em lote (mock do merge)."""
from pathlib import Path

import pytest

import merge as cli
from services.merger import MergeError, MergeResult


@pytest.mark.parametrize("name, key", [
    ("Show.S01E02.1080p.WEB-DL.mkv", "S01E02"),
    ("show s1e2 dublado.mkv", "S01E02"),
    ("Show.S01.E02.mkv", "S01E02"),
    ("Show S01 E02.mkv", "S01E02"),
    ("Show_S01_E02.mkv", "S01E02"),
    ("Show.S02E100.mkv", "S02E100"),
    ("Show.1080p.x265.mkv", None),
    ("Filme (2014).mkv", None),
])
def test_episode_key(name, key):
    assert cli._episode_key(name) == key


def test_scan_episodes(tmp_path):
    s = tmp_path / "Season 1"
    s.mkdir()
    (s / "Show.S01E01.720p.mkv").write_bytes(b"a" * 10)
    (s / "Show.S01E01.1080p.mkv").write_bytes(b"b" * 99)   # maior vence
    (s / "Show.S01E02.mkv").write_bytes(b"c")
    (s / "Show.S01E02.sample.mkv").write_bytes(b"s" * 999)  # sample fica de fora
    (s / "Show.S01E02.srt").write_bytes(b"t")               # não-vídeo fica de fora
    (tmp_path / "notas.txt").write_bytes(b"n")
    eps = cli._scan_episodes(tmp_path)
    assert set(eps) == {"S01E01", "S01E02"}
    assert eps["S01E01"].name == "Show.S01E01.1080p.mkv"


@pytest.fixture
def series_dirs(tmp_path):
    d1, d2, out = tmp_path / "orig", tmp_path / "dub", tmp_path / "out"
    d1.mkdir()
    d2.mkdir()
    for n in ("Show.S01E01.mkv", "Show.S01E02.mkv", "Show.S01E03.mkv"):
        (d1 / n).write_bytes(b"v")
    for n in ("Show S01E02 Dublado.mkv", "Show S01E03 Dublado.mkv", "Show S01E04 Dublado.mkv"):
        (d2 / n).write_bytes(b"d")
    out.mkdir()
    (out / "S01E02.mkv").write_bytes(b"ja existe")  # deve ser pulado
    return d1, d2, out


def test_run_series_skips_existing_and_survives_errors(series_dirs, monkeypatch):
    d1, d2, out = series_dirs
    calls = []

    def fake_merge(f1, f2, output, target_lang=None):
        calls.append((Path(f1).name, Path(f2).name, Path(output).name, target_lang))
        if "S01E03" in output:
            raise MergeError("offset não encontrado")
        Path(output).write_bytes(b"ok")
        return MergeResult(output=output)
    monkeypatch.setattr(cli, "merge", fake_merge)

    failures = cli.run_series(d1, d2, out, "pt")
    # comuns: E02 (pulado, já existe) e E03 (falha simulada) -> 1 falha, 0 merges reais
    assert failures == 1
    assert calls == [("Show.S01E03.mkv", "Show S01E03 Dublado.mkv", "S01E03.mkv", "pt")]
    assert (out / "S01E02.mkv").read_bytes() == b"ja existe"  # não sobrescreveu


def test_run_series_resumes(series_dirs, monkeypatch):
    d1, d2, out = series_dirs
    calls = []

    def ok_merge(f1, f2, output, target_lang=None):
        calls.append(Path(output).name)
        Path(output).write_bytes(b"ok")
        return MergeResult(output=output)
    monkeypatch.setattr(cli, "merge", ok_merge)

    failures = cli.run_series(d1, d2, out, "pt")
    assert failures == 0 and calls == ["S01E03.mkv"]  # E02 já existia; só E03 roda
    assert (out / "S01E03.mkv").exists()


def test_run_series_errors(series_dirs, tmp_path):
    _, d2, out = series_dirs
    with pytest.raises(SystemExit, match="não existe"):
        cli.run_series(Path("/nao/existe"), d2, out, None)

    v1, v2 = tmp_path / "v1", tmp_path / "v2"
    v1.mkdir()
    v2.mkdir()
    (v1 / "Show.S01E01.mkv").write_bytes(b"v")
    (v2 / "Show.S02E01.mkv").write_bytes(b"d")
    with pytest.raises(SystemExit, match="nenhum episódio em comum"):
        cli.run_series(v1, v2, out, None)
