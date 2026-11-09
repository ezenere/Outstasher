"""Nomes de pasta/arquivo de séries no layout padrão do Jellyfin/Plex:

    Série (Ano) [tmdbid-N]/Season 01/Série (Ano) S01E02 [pt+orig].mkv

Mesmo esquema de tag [tmdbid-N] dos filmes (services/catalog.py) — o scanner
do catálogo e o Jellyfin usam a mesma convenção nas duas mídias.
"""
import os
import re
from pathlib import Path

from services import catalog

# "Season 1" -> ("Season ", "1"): prefixo (com a caixa da pasta) e o número
_SEASON_PARTS_RE = re.compile(r"^(.*?)(\d+)$")


def series_folder_name(title: str, year: str | None, tmdb_id: int | None) -> str:
    """Pasta raiz da série no destino — idêntica à de filmes."""
    return catalog.folder_name(title, year, tmdb_id)


def season_dir_name(season: int) -> str:
    """Subpasta da temporada ("Season 01") — o nome que o Jellyfin parseia."""
    return f"Season {season:02d}"


def season_dir_in(series_dir: Path, season: int) -> str:
    """Subpasta da temporada DENTRO de uma pasta de série que já existe.

    A própria, se a pasta já tem esta temporada — outro programa grava
    "Season 1", sem o zero, e criar "Season 01" ao lado deixaria a temporada
    em duas pastas. Temporada nova: no padrão da pasta ("Season 2" ao lado de
    "Season 1"). Especiais (temporada 0) vão para "Specials" se a pasta já
    usa (o nome que o Jellyfin e outros programas dão). Pasta sem subpasta de
    temporada nenhuma: o padrão do Outstasher. Bloqueante (lê a pasta)."""
    default = season_dir_name(season)
    try:
        with os.scandir(series_dir) as it:
            dirs = sorted((e.name for e in it if e.is_dir()), key=str.casefold)
    except OSError:
        return default
    seasons = [(n, catalog.season_of_dir(n)) for n in dirs]
    seasons = [(n, s) for n, s in seasons if s is not None]
    same = [n for n, s in seasons if s == season]
    if same:
        # "Season 1" e "Season 01" juntas: a do padrão, onde as entregas
        # anteriores do Outstasher já caíram
        return default if default in same else same[0]
    if season == 0:
        specials = [n for n in dirs if n.casefold() == "specials"]
        if specials:
            return specials[0]
    if not seasons:
        return default
    prefix, padded, unpadded = None, False, False
    for n, _ in seasons:
        head, digits = _SEASON_PARTS_RE.match(n).groups()
        prefix = prefix or head
        if len(digits) == 1:
            unpadded = True
        elif digits.startswith("0"):
            padded = True
        # "Season 10" não diz nada sobre o zero à esquerda
    width = 1 if unpadded and not padded else 2
    return f"{prefix}{season:0{width}d}"


def episode_file_name(title: str, year: str | None, season: int, episode: int,
                      language: str, tag: str | None = None) -> str:
    """Nome do arquivo final do episódio (sem extensão). O merge produz
    [<lang>+orig]; um lado só, como nos filmes, [orig] ou [<lang>]."""
    safe = catalog.safe_name(f"{title} ({year})" if year else title)
    return f"{safe} S{season:02d}E{episode:02d} [{tag or f'{language}+orig'}]"
