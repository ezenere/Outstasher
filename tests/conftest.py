"""Configuração compartilhada da suíte (pytest).

Filosofia: os testes rodam no AMBIENTE COMPLETO da aplicação — o mesmo onde o
servidor roda (numpy, httpx, python-dotenv, fastapi instalados e ffmpeg/ffprobe
no PATH). Nada é stubado. Se um requisito faltar, a coleção é ABORTADA com uma
mensagem explicando o que instalar/configurar — o teste não é silenciosamente
pulado nem mascarado.

Como rodar (num ambiente com as deps de requirements-dev.txt + ffmpeg no PATH):
    pytest

Fixtures principais:
    temp_db          -> DB_DIR isolado + services.store inicializado (por teste)
    real_encoders    -> encoders REAIS do ffmpeg deste ambiente (frozenset)
    make_media       -> gera arquivos .mkv de teste com ffmpeg
    ffprobe_streams  -> lê as streams de um arquivo via ffprobe
"""
from __future__ import annotations

import importlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# -------------------- checagem hard do ambiente --------------------

_REQUIRED_MODULES = {
    "numpy": "pip install numpy",
    "httpx": "pip install httpx",
    "dotenv": "pip install python-dotenv",
    "fastapi": "pip install fastapi",
}


def _missing_modules() -> list[str]:
    problems = []
    for mod, how in _REQUIRED_MODULES.items():
        try:
            importlib.import_module(mod)
        except ImportError:
            problems.append(f"  - módulo '{mod}' ausente ({how})")
    return problems


def _missing_tools() -> list[str]:
    problems = []
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            problems.append(f"  - '{tool}' não está no PATH (instale o ffmpeg)")
    return problems


def pytest_configure(config):
    """Aborta a suíte inteira se o ambiente não estiver completo, dizendo o
    porquê — em vez de deixar cada teste explodir com um ImportError obscuro."""
    problems = _missing_modules() + _missing_tools()
    if problems:
        raise pytest.UsageError(
            "Ambiente incompleto para rodar a suíte (ela precisa do ambiente\n"
            "COMPLETO da aplicação — o mesmo do servidor, com ffmpeg no PATH).\n"
            "Faltando:\n" + "\n".join(problems) + "\n\n"
            "Instale as dependências de teste e o ffmpeg, e rode de novo:\n"
            "    pip install -r requirements-dev.txt   # + ffmpeg no PATH\n"
            "    pytest")
    config.addinivalue_line("markers",
                            "ffmpeg: exercita ffmpeg de verdade (gera/converte mídia)")


# -------------------- DB isolado por teste --------------------

@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """DB_DIR isolado + services.store inicializado com um banco limpo.

    config e store leem DB_DIR na importação e store mantém a conexão em módulo;
    recarregamos os dois apontando DB_DIR para um tmp_path, sem tocar o jobs.db
    real. Também zeramos o estado in-memory de jobs/catalog (dicts/caches de
    módulo) para o teste não herdar lixo de um teste anterior.
    """
    monkeypatch.setenv("DB_DIR", str(tmp_path))
    import config
    importlib.reload(config)
    from services import store
    importlib.reload(store)
    store.init()

    # zera o estado global dos módulos que os testes exercitam (o reload de
    # store não recria esses módulos, então limpamos à mão)
    from services import jobs, catalog
    jobs._jobs.clear()
    jobs._tasks.clear()
    jobs._ffmpeg_procs.clear()
    jobs._cancelling.clear()
    catalog._library_cache.update(at=0.0, keys=frozenset())

    return store


# -------------------- capacidades reais do ffmpeg --------------------

@pytest.fixture(scope="session")
def real_encoders() -> frozenset[str]:
    """Encoders REAIS do ffmpeg deste ambiente (sem falsear _encoders_cache).

    Os testes de planejamento (transcode) usam isto para se adaptar ao que o
    ffmpeg realmente sabe encodar — em vez de assumir um conjunto fixo.
    """
    from services import transcode
    return transcode.available_encoders()


# -------------------- geração / leitura de mídia --------------------

@pytest.fixture(scope="session")
def make_media():
    """Fábrica de arquivos .mkv de teste (vídeo h264 + N áudios AC3).

    Uso:
        make_media(path, ["eng", "por"])                 # 2 áudios
        make_media(path, ["por"], w=1280, h=536, dur=8)  # resolução/duração
    """
    def _make(path, langs, w=640, h=272, dur=6, video="testsrc"):
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
               "-f", "lavfi", "-i", f"{video}=s={w}x{h}:d={dur}:r=24"]
        for i in range(len(langs)):
            cmd += ["-f", "lavfi", "-i", f"sine=frequency={300 + 100 * i}:duration={dur}"]
        cmd += ["-map", "0:v"]
        for i in range(len(langs)):
            cmd += ["-map", f"{i + 1}:a"]
        cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-b:v", "500k",
                "-pix_fmt", "yuv420p", "-c:a", "ac3", "-b:a", "192k", "-ac", "2"]
        for i, lang in enumerate(langs):
            cmd += [f"-metadata:s:a:{i}", f"language={lang}"]
        cmd += [str(path)]
        subprocess.run(cmd, check=True)
        return path
    return _make


@pytest.fixture(scope="session")
def ffprobe_streams():
    """Lê as streams de um tipo (video/audio/subtitle) de um arquivo."""
    def _streams(path, codec_type):
        from services import merger
        probe = merger.ffprobe_json(str(path))
        return [s for s in probe["streams"] if s.get("codec_type") == codec_type]
    return _streams
