"""Catalogo: le uma pasta de destino e inspeciona os filmes/arquivos.

Estrutura esperada (a que o merge gera): <destino>/<Filme (Ano)>/<arquivos>.
Cada subpasta e um "item" do catalogo. O detalhe roda ffprobe em cada arquivo
de midia e devolve as tracks parseadas de forma detalhada e legivel.
"""
from __future__ import annotations

import re
import shutil
import time
import unicodedata
from pathlib import Path

from services import store
from services.merger import ffprobe_json

VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".m2ts", ".ts", ".mov", ".wmv", ".mpg", ".mpeg", ".webm"}
SUBTITLE_EXTENSIONS = {".srt", ".ass", ".ssa", ".sub", ".idx", ".vtt", ".sup"}
MEDIA_EXTENSIONS = VIDEO_EXTENSIONS | {".m4a", ".mka", ".ac3", ".dts", ".flac", ".mp3", ".aac"}

_YEAR_RE = re.compile(r"\((\d{4})\)")
# [tmdbid-123] no nome da pasta: é assim que o Jellyfin identifica o filme sem
# depender do título (https://jellyfin.org/docs/general/server/media/movies)
_TMDBID_RE = re.compile(r"\s*\[tmdbid-(\d+)\]", re.I)


class CatalogError(Exception):
    pass


# -------------------- helpers de formatacao --------------------

def _human_size(n: int) -> str:
    x = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if x < 1024 or unit == "TB":
            return f"{x:.0f} {unit}" if unit == "B" else f"{x:.2f} {unit}"
        x /= 1024
    return f"{x:.2f} TB"


def _duration(seconds: float | None) -> str | None:
    if not seconds:
        return None
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h{m:02d}m{s:02d}s" if h else f"{m}m{s:02d}s"


def _bitrate(bits_per_s: str | int | None) -> str | None:
    if not bits_per_s:
        return None
    try:
        v = int(bits_per_s)
    except (TypeError, ValueError):
        return None
    if v >= 1_000_000:
        return f"{v / 1_000_000:.1f} Mb/s"
    return f"{v / 1000:.0f} kb/s"


def _title_and_year(folder_name: str) -> tuple[str, str | None]:
    name = _TMDBID_RE.sub("", folder_name)  # o [tmdbid-N] não faz parte do título
    m = _YEAR_RE.search(name)
    year = m.group(1) if m else None
    title = _YEAR_RE.sub("", name).strip(" -.[]") if m else name
    return title.strip(), year


def tmdb_id_in(folder_name: str) -> int | None:
    """O id do TMDB marcado no nome da pasta, se houver."""
    m = _TMDBID_RE.search(folder_name)
    return int(m.group(1)) if m else None


def safe_name(text: str) -> str:
    """Tira o que não pode ir em nome de arquivo/pasta (Windows + POSIX)."""
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", text).strip()


def folder_name(title: str, year: str | None, tmdb_id: int | None = None) -> str:
    """Nome da pasta do filme: 'Título (Ano) [tmdbid-N]'.

    O [tmdbid-N] deixa o Jellyfin identificar o filme sem depender do título —
    útil em remakes, títulos localizados e nomes com pontuação diferente.
    """
    name = safe_name(f"{title} ({year})" if year else title)
    return f"{name} [tmdbid-{tmdb_id}]" if tmdb_id else name


# -------------------- resolucao de destino --------------------

def _resolve_dest(destination_id: int | None) -> dict:
    dest = store.get_destination(destination_id) if destination_id else store.default_destination()
    if dest is None:
        raise CatalogError("Nenhum destino cadastrado")
    return dest


def _safe_join(root: Path, *parts: str) -> Path:
    """Resolve um caminho garantindo que fica dentro de `root` (anti path traversal)."""
    root = root.resolve()
    target = (root / Path(*parts)).resolve()
    if root != target and root not in target.parents:
        raise CatalogError("Caminho fora do destino")
    return target


# -------------------- listagem --------------------

def list_items(destination_id: int | None) -> dict:
    dest = _resolve_dest(destination_id)
    root = Path(dest["path"])
    items = []
    if root.is_dir():
        for entry in sorted(root.iterdir(), key=lambda p: p.name.casefold()):
            if not entry.is_dir():
                continue
            size = 0
            has_video = False
            file_count = 0
            for f in entry.rglob("*"):
                if f.is_file():
                    file_count += 1
                    try:
                        size += f.stat().st_size
                    except OSError:
                        pass
                    if f.suffix.lower() in VIDEO_EXTENSIONS:
                        has_video = True
            title, year = _title_and_year(entry.name)
            items.append({
                "folder": entry.name,
                "title": title,
                "year": year,
                "size": size,
                "size_human": _human_size(size),
                "file_count": file_count,
                "has_video": has_video,
            })
    return {
        "destination": dest,
        "exists": root.is_dir(),
        "items": items,
    }


# -------------------- detalhe (ffprobe parseado) --------------------

def _parse_stream(s: dict) -> dict:
    tags = s.get("tags") or {}
    disp = s.get("disposition") or {}
    kind = s.get("codec_type")
    common = {
        "index": s.get("index"),
        "type": kind,
        "codec": s.get("codec_name"),
        "codec_long": s.get("codec_long_name"),
        "profile": s.get("profile"),
        "language": tags.get("language"),
        "title": tags.get("title"),
        "default": disp.get("default") == 1,
        "forced": disp.get("forced") == 1,
        "bitrate": _bitrate(s.get("bit_rate")),
        "raw": {},  # campos extras "crus" para quem quiser ver tudo
    }

    if kind == "video":
        w, h = s.get("width"), s.get("height")
        fr = s.get("avg_frame_rate") or s.get("r_frame_rate")
        fps = None
        if fr and "/" in fr:
            num, den = fr.split("/")
            fps = round(int(num) / int(den), 3) if int(den) else None
        common.update({
            "resolution": f"{w}x{h}" if w and h else None,
            "width": w, "height": h,
            "fps": fps,
            "pix_fmt": s.get("pix_fmt"),
            "bit_depth": s.get("bits_per_raw_sample"),
            "color_space": s.get("color_space"),
            "color_transfer": s.get("color_transfer"),
            "color_primaries": s.get("color_primaries"),
            "hdr": (s.get("color_transfer") in ("smpte2084", "arib-std-b67")),
            "aspect_ratio": s.get("display_aspect_ratio"),
            "level": s.get("level"),
        })
    elif kind == "audio":
        common.update({
            "channels": s.get("channels"),
            "channel_layout": s.get("channel_layout"),
            "sample_rate": f"{int(s['sample_rate']) / 1000:.1f} kHz" if s.get("sample_rate") else None,
            "sample_fmt": s.get("sample_fmt"),
        })
    elif kind == "subtitle":
        common.update({
            "hearing_impaired": disp.get("hearing_impaired") == 1,
        })

    # guarda campos extras crus que nao foram explicitamente mapeados
    mapped = {"index", "codec_type", "codec_name", "codec_long_name", "profile",
              "tags", "disposition", "bit_rate", "width", "height", "avg_frame_rate",
              "r_frame_rate", "pix_fmt", "bits_per_raw_sample", "color_space",
              "color_transfer", "color_primaries", "display_aspect_ratio", "level",
              "channels", "channel_layout", "sample_rate", "sample_fmt"}
    common["raw"] = {k: v for k, v in s.items() if k not in mapped and not isinstance(v, (dict, list))}
    return common


def _probe_file(path: Path) -> dict:
    info: dict = {
        "name": path.name,
        "ext": path.suffix.lower(),
        "size": 0,
        "size_human": "?",
    }
    try:
        info["size"] = path.stat().st_size
        info["size_human"] = _human_size(info["size"])
    except OSError:
        pass

    is_media = path.suffix.lower() in MEDIA_EXTENSIONS
    is_sub = path.suffix.lower() in SUBTITLE_EXTENSIONS
    info["category"] = "video" if path.suffix.lower() in VIDEO_EXTENSIONS else (
        "subtitle" if is_sub else ("media" if is_media else "other"))

    if not (is_media or path.suffix.lower() in {".vtt", ".srt", ".ass", ".ssa"}):
        return info

    try:
        probe = ffprobe_json(str(path))
    except Exception as e:  # noqa: BLE001 - arquivo pode nao ser sondavel
        info["probe_error"] = str(e)
        return info

    fmt = probe.get("format") or {}
    info["container"] = fmt.get("format_long_name") or fmt.get("format_name")
    info["duration"] = _duration(float(fmt["duration"])) if fmt.get("duration") else None
    info["overall_bitrate"] = _bitrate(fmt.get("bit_rate"))
    streams = [_parse_stream(s) for s in probe.get("streams", [])]
    info["streams"] = streams
    info["counts"] = {
        "video": sum(1 for s in streams if s["type"] == "video"),
        "audio": sum(1 for s in streams if s["type"] == "audio"),
        "subtitle": sum(1 for s in streams if s["type"] == "subtitle"),
    }
    chapters = probe.get("chapters") or []
    info["chapters"] = len(chapters)
    return info


def item_detail(destination_id: int | None, folder: str) -> dict:
    dest = _resolve_dest(destination_id)
    root = Path(dest["path"])
    item_dir = _safe_join(root, folder)
    if not item_dir.is_dir():
        raise CatalogError("Pasta do filme não encontrada")

    title, year = _title_and_year(folder)
    files = []
    total = 0
    for f in sorted(item_dir.rglob("*"), key=lambda p: p.name.casefold()):
        if f.is_file():
            info = _probe_file(f)
            info["rel"] = f.relative_to(item_dir).as_posix()
            total += info["size"]
            files.append(info)

    return {
        "destination": dest,
        "folder": folder,
        "title": title,
        "year": year,
        "size": total,
        "size_human": _human_size(total),
        "files": files,
    }


# -------------------- remocao --------------------

def delete_file(destination_id: int | None, folder: str, rel: str) -> None:
    dest = _resolve_dest(destination_id)
    root = Path(dest["path"])
    target = _safe_join(root, folder, rel)
    if not target.is_file():
        raise CatalogError("Arquivo não encontrado")
    target.unlink()


def rename_file(destination_id: int | None, folder: str, rel: str, new_name: str) -> str:
    """Renomeia um arquivo do item, mantendo-o na MESMA subpasta.

    `new_name` é só o nome do arquivo (sem barras) — não move entre pastas. Se
    vier sem extensão, herda a do arquivo original (para o usuário não perder o
    .mkv sem querer). Retorna o novo `rel` (relativo à pasta do item).
    """
    dest = _resolve_dest(destination_id)
    root = Path(dest["path"])
    item_dir = _safe_join(root, folder)
    src = _safe_join(root, folder, rel)
    if not src.is_file():
        raise CatalogError("Arquivo não encontrado")

    clean = (new_name or "").strip()
    if not clean:
        raise CatalogError("Nome vazio")
    # o novo nome é só o nome do arquivo — nada de mudar de pasta por aqui
    if "/" in clean or "\\" in clean or clean in (".", ".."):
        raise CatalogError("O nome não pode conter barras")
    # caracteres proibidos em nomes de arquivo (Windows/POSIX) + controle
    if re.search(r'[<>:"|?*\x00-\x1f]', clean):
        raise CatalogError('O nome não pode conter < > : " | ? * nem controle')
    if not Path(clean).suffix:  # sem extensão: herda a do original
        clean += src.suffix

    target = _safe_join(root, folder, src.parent.relative_to(item_dir).as_posix(), clean)
    if target == src:
        return src.relative_to(item_dir).as_posix()
    if target.exists():
        raise CatalogError(f"Já existe um arquivo chamado '{clean}' nesta pasta")
    src.rename(target)
    return target.relative_to(item_dir).as_posix()


def media_path(destination_id: int | None, folder: str, rel: str) -> Path:
    """Caminho absoluto de um arquivo de VÍDEO do item (para recomprimir)."""
    dest = _resolve_dest(destination_id)
    target = _safe_join(Path(dest["path"]), folder, rel)
    if not target.is_file():
        raise CatalogError("Arquivo não encontrado")
    if target.suffix.lower() not in VIDEO_EXTENSIONS:
        raise CatalogError(f"'{target.name}' não é um arquivo de vídeo")
    return target


def rename_folder(destination_id: int | None, folder: str, new_name: str) -> str:
    """Renomeia a pasta do filme dentro do mesmo destino. Retorna o novo nome."""
    dest = _resolve_dest(destination_id)
    root = Path(dest["path"])
    src = _safe_join(root, folder)
    if not src.is_dir():
        raise CatalogError("Pasta não encontrada")
    raw = (new_name or "").strip()
    # rejeita em vez de sanear: um nome com barra/'..' é engano de quem chamou,
    # e renomear para uma versão "limpa" dele seria surpresa (não segurança)
    if "/" in raw or "\\" in raw or raw in (".", ".."):
        raise CatalogError("O nome da pasta não pode conter barras")
    clean = safe_name(raw)
    if not clean:
        raise CatalogError("Nome vazio")
    target = _safe_join(root, clean)
    if target == src:
        return folder
    if target.exists():
        raise CatalogError(f"Já existe uma pasta chamada '{clean}'")
    src.rename(target)
    invalidate_library()  # o nome da pasta alimenta o cache da coleção
    return clean


def tag_folder_with_tmdb(destination_id: int | None, folder: str, tmdb_id: int) -> str:
    """Marca a pasta com [tmdbid-N] (o Jellyfin usa isso para identificar o
    filme). Se já houver um id no nome, é substituído."""
    title, year = _title_and_year(folder)
    return rename_folder(destination_id, folder, folder_name(title, year, tmdb_id))


def delete_folder(destination_id: int | None, folder: str) -> None:
    dest = _resolve_dest(destination_id)
    root = Path(dest["path"])
    target = _safe_join(root, folder)
    if target == root.resolve():
        raise CatalogError("Não é possível remover a raiz do destino")
    if not target.is_dir():
        raise CatalogError("Pasta não encontrada")
    shutil.rmtree(target)
    invalidate_library()  # sumiu um filme da coleção


# -------------------- cache "já está na coleção?" --------------------
# A busca de filmes (TMDB) marca o que o usuário JÁ TEM nos destinos. Para não
# bater no disco a cada busca: um scan leve (só o 1º nível das pastas de cada
# destino) fica em memória por LIBRARY_TTL_SECONDS e só é refeito quando uma
# busca chega com o cache vencido (on demand) ou após invalidate_library()
# (mudança real: job concluído, pasta removida, destinos alterados).
# Sem lock de propósito: duas buscas simultâneas com cache vencido só fariam
# o mesmo scan duas vezes — inofensivo, e a atribuição do dict é atômica.

# "at" usa time.monotonic(), que conta desde o boot — 0.0 não serve de
# sentinela de "vencido" (numa máquina de pé há < TTL o cache vazio pareceria
# fresco). None = nunca escaneado / invalidado.
LIBRARY_TTL_SECONDS = 30 * 60
_library_cache: dict = {"at": None, "keys": frozenset()}


def _norm_title(text: str) -> str:
    """Título normalizado para casar pasta com TMDB: minúsculo, sem acentos,
    só letras/números ('WALL·E' == 'WALLE', 'Tóquio' == 'toquio')."""
    nfkd = unicodedata.normalize("NFKD", (text or "").lower())
    stripped = "".join(c for c in nfkd if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "", stripped)


def _scan_library() -> frozenset[tuple[str, str | None]]:
    """(titulo_normalizado, ano|None) de cada pasta de filme em cada destino."""
    keys = set()
    for dest in store.list_destinations():
        root = Path(dest["path"])
        try:
            if not root.is_dir():
                continue
            for entry in root.iterdir():
                if entry.is_dir():
                    title, year = _title_and_year(entry.name)
                    if title:
                        keys.add((_norm_title(title), year))
        except OSError:
            continue  # destino desmontado/sem permissão não derruba a busca
    return frozenset(keys)


def library_keys() -> frozenset[tuple[str, str | None]]:
    """Chaves da coleção, do cache (rescan só se venceu o TTL). Bloqueante —
    chamar via asyncio.to_thread na API."""
    at = _library_cache["at"]
    if at is None or time.monotonic() - at > LIBRARY_TTL_SECONDS:
        _library_cache["keys"] = _scan_library()
        _library_cache["at"] = time.monotonic()
    return _library_cache["keys"]


def invalidate_library() -> None:
    """Marca o cache como vencido (a PRÓXIMA busca refaz o scan — nada roda já)."""
    _library_cache["at"] = None


def in_library(movie: dict, keys: frozenset) -> bool:
    """O filme do TMDB já está na coleção? Casa título original OU localizado
    + ano; pasta sem ano no nome casa só pelo título."""
    year = movie.get("year") or None
    for t in (movie.get("original_title"), movie.get("title")):
        if not t:
            continue
        norm = _norm_title(t)
        if not norm:
            continue
        if (norm, year) in keys or (norm, None) in keys:
            return True
    return False
