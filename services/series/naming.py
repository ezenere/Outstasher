"""Nomes de pasta/arquivo de séries no layout padrão do Jellyfin/Plex:

    Série (Ano) [tmdbid-N]/Season 01/Série (Ano) S01E02 [pt+orig].mkv

Mesmo esquema de tag [tmdbid-N] dos filmes (services/catalog.py) — o scanner
do catálogo e o Jellyfin usam a mesma convenção nas duas mídias.
"""
from services import catalog


def series_folder_name(title: str, year: str | None, tmdb_id: int | None) -> str:
    """Pasta raiz da série no destino — idêntica à de filmes."""
    return catalog.folder_name(title, year, tmdb_id)


def season_dir_name(season: int) -> str:
    """Subpasta da temporada ("Season 01") — o nome que o Jellyfin parseia."""
    return f"Season {season:02d}"


def episode_file_name(title: str, year: str | None, season: int, episode: int,
                      language: str, tag: str | None = None) -> str:
    """Nome do arquivo final do episódio (sem extensão). O merge produz
    [<lang>+orig]; um lado só, como nos filmes, [orig] ou [<lang>]."""
    safe = catalog.safe_name(f"{title} ({year})" if year else title)
    return f"{safe} S{season:02d}E{episode:02d} [{tag or f'{language}+orig'}]"
