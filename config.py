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

# DB_DIR permite pôr o jobs.db num volume (Docker) fora da imagem. Sem ele,
# fica ao lado do codigo (comportamento local de sempre).
DB_DIR = Path(os.getenv("DB_DIR", str(BASE_DIR)))
DB_FILE = DB_DIR / "jobs.db"
JOBS_FILE = DB_DIR / "jobs.json"  # formato antigo; migrado para o SQLite no boot


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


def _env_bool(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes", "on")


# SVT-AV1 (AV1 por software) segura frames em buffer proporcional ao lookahead;
# em 4K 10-bit isso passa de 12 GB e o kernel mata o ffmpeg (OOM). Por padrão o
# app limita o lookahead à RAM da máquina. Defina IGNORE_AV1_LOOKAHEAD_LIMITS=true
# para DESLIGAR esse teto e deixar o SVT-AV1 usar o próprio default — só faça
# isso se a máquina tiver RAM de sobra ou você encodar abaixo de 4K, senão o
# encode volta a arriscar OOM. Ver services/transcode.svtav1_lookahead().
IGNORE_AV1_LOOKAHEAD_LIMITS = _env_bool("IGNORE_AV1_LOOKAHEAD_LIMITS")

# quão rápido o watchdog consulta o qBittorrent (atualiza o progresso em
# memória). Fica abaixo do tick de 1s do detalhe do job na UI, para a barra
# sempre ler um valor fresco.
POLL_INTERVAL_SECONDS = 0.9
# de quanto em quanto tempo o progresso é PERSISTIDO no banco. Consultar o
# qBittorrent é barato, mas gravar no SQLite a cada tick é desperdício — o
# progresso é só um número que a UI já lê do backend em memória. Eventos reais
# (download concluído, troca de torrent, warning...) persistem na hora via
# _event; aqui é só o "mero progresso" que fica throttled.
PROGRESS_PERSIST_SECONDS = 60

# Idiomas suportados para a faixa dublada.
# tmdb: codigo de idioma usado para buscar o titulo traduzido no TMDB.
# markers_strong: marcadores que CONFIRMAM dublagem no idioma ("dublado",
#   grupos/sites nacionais, "dual áudio" com acento...) — ganham bônus de score.
# markers_weak: ambiguos ("dual", "multi" podem ser quaisquer idiomas) — contam
#   como marcador só por falta de opção melhor, sem bônus.
# Comparados em minúsculas, MANTENDO acentos: "dual áudio" (com acento) é sinal
# de release brasileiro; "dual audio" (sem) pode ser Hindi+English.
#
# ATENÇÃO: estes são apenas os valores PADRÃO usados para popular o banco na
# primeira execução (store.seed_languages). Em runtime, LANGUAGES e
# SUBTITLE_MARKERS são carregados do banco por store.load_language_config() e
# podem ser editados pela UI. Não leia _DEFAULT_* diretamente no código —
# use config.LANGUAGES / config.SUBTITLE_MARKERS.
_DEFAULT_LANGUAGES = {
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

# Marcadores de LEGENDA (universais). Se o titulo tem algum destes e NENHUM
# marcador de dublagem/dual do idioma alvo, o video tem audio ORIGINAL (so
# legendado) — nao serve como faixa dublada. Comparados sem acento (_fold).
_DEFAULT_SUBTITLE_MARKERS = [
    "legendado", "legenda", "legendas", "leg",
    "subbed", "subtitled", "subtitle", "subtitles", "subs",
    "subtitulado", "subtitulada", "sottotitolato", "untertitel",
    "vose", "vostfr", "ost",  # versao original + legenda (es/fr)
]

# ---- valores em runtime (populados do banco em store.load_language_config) ----
# começam com os padrões para o caso raro de algo ler antes do load (ex.: testes
# que não chamam store.init()); o load real substitui pelos valores do banco.
LANGUAGES = {k: dict(v, markers_strong=list(v["markers_strong"]),
                     markers_weak=list(v["markers_weak"]))
             for k, v in _DEFAULT_LANGUAGES.items()}
SUBTITLE_MARKERS = list(_DEFAULT_SUBTITLE_MARKERS)


def install_language_config(languages: dict, subtitle_markers: list):
    """Substitui os valores em runtime (chamado pelo store após ler/editar o banco).

    Muta os objetos LANGUAGES/SUBTITLE_MARKERS no lugar para que quem importou
    `from config import LANGUAGES` continue enxergando os valores atualizados.
    """
    LANGUAGES.clear()
    LANGUAGES.update(languages)
    SUBTITLE_MARKERS.clear()
    SUBTITLE_MARKERS.extend(subtitle_markers)
