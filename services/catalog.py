"""Catalogo: le uma pasta de destino e inspeciona os filmes/arquivos.

Estrutura esperada (a que o merge gera): <destino>/<Filme (Ano)>/<arquivos>.
Cada subpasta e um "item" do catalogo. O detalhe roda ffprobe em cada arquivo
de midia e devolve as tracks parseadas de forma detalhada e legivel.
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path

from services import store
from services.merger import ffprobe_json

VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".m2ts", ".ts", ".mov", ".wmv", ".mpg", ".mpeg", ".webm"}
SUBTITLE_EXTENSIONS = {".srt", ".ass", ".ssa", ".sub", ".idx", ".vtt", ".sup"}
MEDIA_EXTENSIONS = VIDEO_EXTENSIONS | {".m4a", ".mka", ".ac3", ".dts", ".flac", ".mp3", ".aac"}

_YEAR_RE = re.compile(r"\((\d{4})\)")


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
    m = _YEAR_RE.search(folder_name)
    year = m.group(1) if m else None
    title = _YEAR_RE.sub("", folder_name).strip(" -.[]") if m else folder_name
    return title.strip(), year


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


def delete_folder(destination_id: int | None, folder: str) -> None:
    dest = _resolve_dest(destination_id)
    root = Path(dest["path"])
    target = _safe_join(root, folder)
    if target == root.resolve():
        raise CatalogError("Não é possível remover a raiz do destino")
    if not target.is_dir():
        raise CatalogError("Pasta não encontrada")
    shutil.rmtree(target)
