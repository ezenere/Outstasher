import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")

TMDB_API_KEY = os.getenv("TMDB_API_KEY", "")
JACKETT_URL = os.getenv("JACKETT_URL", "http://localhost:9117").rstrip("/")
JACKETT_API_KEY = os.getenv("JACKETT_API_KEY", "")
QBIT_URL = os.getenv("QBIT_URL", "http://localhost:8080").rstrip("/")
QBIT_USER = os.getenv("QBIT_USER", "admin")
QBIT_PASS = os.getenv("QBIT_PASS", "")
# OUTPUT_DIR do .env vira o destino "Padrão" na primeira execucao; depois os
# destinos sao gerenciados pela UI (tabela destinations no jobs.db).
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", str(BASE_DIR / "output")))
DB_FILE = BASE_DIR / "jobs.db"
JOBS_FILE = BASE_DIR / "jobs.json"  # formato antigo; migrado para o SQLite no boot


def _parse_path_map(raw: str) -> list[tuple[str, str]]:
    """"/downloads=>/mnt/nas/downloads;/outro=>/mnt/outro" -> [(src, dst), ...]"""
    mappings = []
    for part in raw.split(";"):
        if "=>" in part:
            src, dst = part.split("=>", 1)
            if src.strip() and dst.strip():
                mappings.append((src.strip(), dst.strip()))
    # prefixos mais longos primeiro, para o match mais especifico ganhar
    return sorted(mappings, key=lambda m: len(m[0]), reverse=True)


def map_path(path: str, mappings: list[tuple[str, str]]) -> str:
    """Aplica uma traducao de prefixo de caminho (separadores normalizados)."""
    norm = path.replace("\\", "/")
    for src, dst in mappings:
        s = src.replace("\\", "/").rstrip("/")
        if norm == s or norm.startswith(s + "/"):
            return dst.rstrip("/\\") + norm[len(s):]
    return path


# Traducao dos caminhos reportados pelo qBittorrent (que roda em outra maquina)
# para onde a mesma pasta esta montada na maquina deste servidor.
QBIT_PATH_MAP = _parse_path_map(os.getenv("QBIT_PATH_MAP", ""))

# Pasta onde o qBittorrent deve salvar os torrents (do ponto de vista DELE).
# Vazio = pasta padrao do qBittorrent.
QBIT_SAVE_PATH = os.getenv("QBIT_SAVE_PATH", "").strip()

# Watchdog: minutos sem progresso antes de trocar o torrent pelo proximo candidato.
# 0 desativa.
STALL_TIMEOUT_MINUTES = int(os.getenv("STALL_TIMEOUT_MINUTES", "15") or "15")

# O que fazer com os torrents apos o merge: keep | remove | remove_data
QBIT_CLEANUP = (os.getenv("QBIT_CLEANUP", "keep").strip() or "keep").lower()

POLL_INTERVAL_SECONDS = 15

# Idiomas suportados para a faixa dublada.
# tmdb: codigo de idioma usado para buscar o titulo traduzido no TMDB.
# markers_strong: marcadores que CONFIRMAM dublagem no idioma ("dublado",
#   grupos/sites nacionais, "dual áudio" com acento...) — ganham bônus de score.
# markers_weak: ambiguos ("dual", "multi" podem ser quaisquer idiomas) — contam
#   como marcador só por falta de opção melhor, sem bônus.
# Comparados em minúsculas, MANTENDO acentos: "dual áudio" (com acento) é sinal
# de release brasileiro; "dual audio" (sem) pode ser Hindi+English.
LANGUAGES = {
    "pt": {
        "label": "Português",
        "tmdb": "pt-BR",
        "markers_strong": ["dublado", "dublagem", "nacional", "portugues",
                           "português", "pt-br", "ptbr", "pt br", "brazilian",
                           "bludv", "dual áudio", "áudio dual", "filmes"],
        "markers_weak": ["dual"],
    },
    "es": {
        "label": "Espanhol",
        "tmdb": "es-ES",
        "markers_strong": ["castellano", "español", "espanol", "latino",
                           "spanish", "esp"],
        "markers_weak": ["dual", "multi"],
    },
    "it": {
        "label": "Italiano",
        "tmdb": "it-IT",
        "markers_strong": ["italian", "ita"],
        "markers_weak": ["dual", "multi"],
    },
    "de": {
        "label": "Alemão",
        "tmdb": "de-DE",
        "markers_strong": ["german", "deutsch", "ger"],
        "markers_weak": ["dual", "multi"],
    },
    "fr": {
        "label": "Francês",
        "tmdb": "fr-FR",
        "markers_strong": ["french", "truefrench", "vff", "vf ", "fre"],
        "markers_weak": ["dual", "multi"],
    },
}
