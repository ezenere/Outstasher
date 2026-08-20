"""Navegador de pastas do servidor (leitura), para escolher caminhos na UI.

Genérico de propósito: qualquer tela que precise de um caminho usa a mesma
rota. Nunca escreve nada — só lista diretórios e arquivos de mídia.

O ponto de partida não é `/` por acaso: quem usa o serviço mexe em duas
árvores (onde os torrents caem e onde a coleção mora), então elas vêm como
ATALHOS. `/` continua acessível para o resto.
"""
from pathlib import Path

import config
from services import store

# extensões que aparecem na listagem (o resto é ruído para o que a UI faz)
MEDIA_EXTENSIONS = {".mkv", ".mp4", ".avi", ".m2ts", ".mov", ".wmv", ".mpg",
                    ".mpeg", ".ts", ".webm"}
MAX_ENTRIES = 2000     # pasta gigante não derruba a UI (nem a resposta)


class BrowseError(RuntimeError):
    pass


def shortcuts() -> list[dict]:
    """Atalhos: destinos da coleção + caminhos locais dos destinos de torrent.

    São os lugares de onde os arquivos realmente saem; começar neles poupa
    dezenas de cliques a partir de `/`.
    """
    out: list[dict] = []
    seen: set[str] = set()
    try:
        alvos, destinos = store.list_torrent_targets(), store.list_destinations()
    except Exception:      # noqa: BLE001 — banco ainda não aberto: sem atalhos
        alvos, destinos = [], []

    def add(label: str, path: str | None):
        if not path:
            return
        p = str(Path(path))
        if p in seen or not Path(p).is_dir():
            return
        seen.add(p)
        out.append({"label": label, "path": p})

    for t in alvos:
        add(f"Torrents: {t['label']}", t.get("local_path") or t.get("save_path"))
    for d in destinos:
        add(f"Coleção: {d['label']}", d.get("path"))
    add("Saída padrão", str(config.OUTPUT_DIR))
    return out


def default_path() -> str:
    """Onde a navegação começa: o primeiro atalho que existir, senão `/`."""
    atalhos = shortcuts()
    return atalhos[0]["path"] if atalhos else "/"


def list_dir(path: str | None = None) -> dict:
    """Conteúdo de um diretório: subpastas + arquivos de mídia.

    Diretório inacessível vira erro claro (a UI mostra e deixa voltar), não
    uma lista vazia — sem isso, um caminho errado parece "pasta vazia".
    """
    alvo = Path(path).expanduser() if path else Path(default_path())
    try:
        alvo = alvo.resolve()
    except OSError as e:
        raise BrowseError(f"caminho inválido: {e}")
    if not alvo.exists():
        raise BrowseError(f"'{alvo}' não existe nesta máquina")
    if not alvo.is_dir():
        raise BrowseError(f"'{alvo}' não é uma pasta")

    dirs: list[dict] = []
    files: list[dict] = []
    truncated = False
    try:
        for i, entry in enumerate(sorted(alvo.iterdir(), key=_sort_key)):
            if i >= MAX_ENTRIES:
                truncated = True
                break
            if entry.name.startswith("."):
                continue
            try:
                if entry.is_dir():
                    dirs.append({"name": entry.name, "path": str(entry)})
                elif entry.suffix.lower() in MEDIA_EXTENSIONS:
                    files.append({"name": entry.name, "path": str(entry),
                                  "size": entry.stat().st_size})
            except OSError:
                continue   # entrada ilegível (permissão, link quebrado): pula
    except PermissionError:
        raise BrowseError(f"sem permissão para ler '{alvo}'")
    except OSError as e:
        raise BrowseError(f"não consegui ler '{alvo}': {e}")

    return {
        "path": str(alvo),
        "parent": str(alvo.parent) if alvo.parent != alvo else None,
        "dirs": dirs,
        "files": files,
        "truncated": truncated,
        "shortcuts": shortcuts(),
    }


def _sort_key(p: Path):
    """Pastas antes de arquivos, cada grupo em ordem natural (case-insensitive)."""
    try:
        is_dir = p.is_dir()
    except OSError:
        is_dir = False
    return (not is_dir, p.name.lower())
