"""Dono/permissão do que o app grava (PUID/PGID/FILE_MODE/DIR_MODE)."""
import grp
import os
import stat
from pathlib import Path

import pytest

from services import fsperm, merger


def _mode(p: Path) -> int:
    return stat.S_IMODE(os.lstat(p).st_mode)


@pytest.fixture
def regras(monkeypatch):
    """Regra com dono/grupo do próprio usuário (chown para si mesmo não
    precisa de root) e um grupo secundário dele, se houver."""
    gid = next((g for g in os.getgroups() if g != os.getgid()), os.getgid())
    monkeypatch.setenv("PUID", str(os.getuid()))
    monkeypatch.setenv("PGID", str(gid))
    monkeypatch.setenv("FILE_MODE", "775")
    monkeypatch.setenv("DIR_MODE", "0775")
    return gid


@pytest.fixture(autouse=True)
def _limpa(monkeypatch):
    for k in ("PUID", "PGID", "FILE_MODE", "DIR_MODE"):
        monkeypatch.delenv(k, raising=False)
    fsperm._warned.clear()


def test_sem_regra_nao_mexe(tmp_path):
    f = tmp_path / "a.mkv"
    f.write_bytes(b"x")
    os.chmod(f, 0o600)
    fsperm.apply(f)
    assert _mode(f) == 0o600
    assert fsperm.describe().startswith("permissões: sem regra")


def test_arquivo_recebe_modo_e_grupo(tmp_path, regras):
    f = tmp_path / "a.mkv"
    f.write_bytes(b"x")
    os.chmod(f, 0o644)
    fsperm.apply(f)
    assert _mode(f) == 0o775
    assert os.stat(f).st_gid == regras


def test_makedirs_so_nas_pastas_que_criou(tmp_path, regras, monkeypatch):
    existente = tmp_path / "filmes"
    existente.mkdir()
    os.chmod(existente, 0o700)
    monkeypatch.setenv("DIR_MODE", "770")
    alvo = existente / "Filme Exemplo (2020)" / "extras"
    fsperm.makedirs(alvo)
    assert alvo.is_dir()
    assert _mode(existente) == 0o700           # já existia: intocada
    assert _mode(existente / "Filme Exemplo (2020)") == 0o770
    assert _mode(alvo) == 0o770
    fsperm.makedirs(alvo)                      # de novo: nada novo, nada muda


def test_valor_invalido_vira_aviso_e_e_ignorado(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("FILE_MODE", "rwxrwxr-x")
    monkeypatch.setenv("PGID", "grupo-que-nao-existe-aqui")
    f = tmp_path / "a.srt"
    f.write_text("x")
    os.chmod(f, 0o640)
    fsperm.apply(f)
    assert _mode(f) == 0o640
    assert "FILE_MODE" in caplog.text and "PGID" in caplog.text


def test_nome_de_grupo_resolve(monkeypatch):
    nome = grp.getgrgid(os.getgid()).gr_name
    monkeypatch.setenv("PGID", nome)
    assert fsperm.rules()["gid"] == os.getgid()


def test_chown_negado_nao_derruba(tmp_path, monkeypatch, caplog):
    if os.getuid() == 0:
        pytest.skip("root pode tudo")
    monkeypatch.setenv("PUID", "0")
    monkeypatch.setenv("FILE_MODE", "664")
    f = tmp_path / "a.mkv"
    f.write_bytes(b"x")
    fsperm.apply(f)                            # não levanta
    assert _mode(f) == 0o664                   # o chmod ainda vale
    assert "chown" in caplog.text


def test_symlink_nao_segue_para_o_alvo(tmp_path, regras):
    alvo = tmp_path / "alvo.mkv"
    alvo.write_bytes(b"x")
    os.chmod(alvo, 0o600)
    link = tmp_path / "link.mkv"
    link.symlink_to(alvo)
    fsperm.apply(link)
    assert _mode(alvo) == 0o600


def test_link_or_copy_aplica_a_regra(tmp_path, regras):
    src = tmp_path / "torrent" / "Filme.Exemplo.mkv"
    src.parent.mkdir()
    src.write_bytes(b"x" * 10)
    os.chmod(src, 0o644)
    dst = tmp_path / "filmes" / "Filme Exemplo (2020)" / "Filme Exemplo (2020).mkv"
    merger._link_or_copy(src, dst, [])
    assert _mode(dst) == 0o775
    assert _mode(dst.parent) == 0o775
    assert os.stat(dst).st_gid == regras
