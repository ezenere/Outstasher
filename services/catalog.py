"""Catalogo: le uma pasta de destino e inspeciona os filmes/arquivos.

Estrutura esperada (a que o merge gera): <destino>/<Filme (Ano)>/<arquivos>.
Cada subpasta e um "item" do catalogo. O detalhe roda ffprobe em cada arquivo
de midia e devolve as tracks parseadas de forma detalhada e legivel.
"""
from __future__ import annotations

import difflib
import os
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

def human_size(n: int) -> str:
    """Tamanho legível (B/KB/MB/GB/TB). Público: os jobs também usam."""
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


# -------------------- séries --------------------

# subpasta de temporada no layout Jellyfin ("Season 01"); aceita 1-2 dígitos
_SEASON_DIR_RE = re.compile(r"^Season (\d{1,2})$", re.I)
# referência de episódio no NOME DO ARQUIVO (SxxEyy) — para o índice de
# episódios presentes e o agrupamento do detalhe
_EPISODE_FILE_RE = re.compile(r"[sS](\d{1,2})[ ._-]?[eE](\d{1,3})")
# o mesmo SxxEyy com a cauda de multi-episódio dos nomes de outros programas:
# "S01E01-E02", "S01E01E02", "S01E01-02-03". A forma sem o E ("-02") só vale
# colada e seguida de não-alfanumérico — senão "S01E01-1080p" e "S01E09-10bit"
# virariam episódios. Com espaço ("S01E01 - 02 Título") é título, não range.
_EPISODE_MULTI_RE = re.compile(
    r"[sS](\d{1,2})[ ._-]?[eE](\d{1,3})"
    r"((?:-?[eE]\d{1,3}|-\d{1,3}(?![\dA-Za-z]))*)")
_TAIL_NUM_RE = re.compile(r"\d{1,3}")
# "1x02" (scene antiga): exatamente 2 dígitos e sem dígito/x antes — "1920x1080"
# não casa (mesma regra de services/series/parse)
_NXMM_FILE_RE = re.compile(r"(?<![\dxX])(\d{1,2})[xX](\d{2})(?!\d)")
# só DENTRO de uma pasta "Season NN" (a temporada vem da pasta): "Episode 05",
# "Ep05", "E05 - Título" ou "05 - Título" — nomes que o Jellyfin também aceita.
# O "E" solto só colado no número: "Amor e 2 Amigos" não é o episódio 2
_EP_IN_SEASON_RE = re.compile(
    r"(?:^|[ ._\-\[(])(?:(?:episode|episodio|episódio|ep)[ ._-]?|e)(\d{1,3})(?!\d)",
    re.I)
_LEADING_EP_RE = re.compile(r"^(\d{1,3})(?!\d)(?=[ ._\-])")
# teto de um range de multi-episódio: arquivo com 20+ episódios é erro de parse
_MAX_EPISODES_PER_FILE = 20


def season_of_dir(name: str) -> int | None:
    m = _SEASON_DIR_RE.match(name)
    return int(m.group(1)) if m else None


def parse_episode_filename(name: str) -> tuple[int, int] | None:
    """(temporada, episódio) do nome do arquivo, ou None."""
    m = _EPISODE_FILE_RE.search(name)
    return (int(m.group(1)), int(m.group(2))) if m else None


def episodes_in_filename(name: str, season: int | None = None) -> set[tuple[int, int]]:
    """TODOS os (temporada, episódio) que um arquivo contém — para o índice de
    episódios presentes na coleção.

    Além do "S01E02" que o Outstasher grava, entende o que outros programas
    deixam no destino: multi-episódio ("S01E01-E02" = dois episódios num
    arquivo; ignorar o segundo o faria parecer faltando e ser baixado de
    novo), "1x02", e — com `season` vindo da pasta "Season NN" — nomes sem
    temporada ("Episode 05", "05 - Título")."""
    out: set[tuple[int, int]] = set()
    for m in _EPISODE_MULTI_RE.finditer(name):
        s, first = int(m.group(1)), int(m.group(2))
        last = max([first] + [int(x) for x in _TAIL_NUM_RE.findall(m.group(3))])
        if last - first >= _MAX_EPISODES_PER_FILE:
            last = first
        out.update((s, e) for e in range(first, last + 1))
    if out:
        return out
    for m in _NXMM_FILE_RE.finditer(name):
        out.add((int(m.group(1)), int(m.group(2))))
    if out or season is None:
        return out
    stem = Path(name).stem
    m = _EP_IN_SEASON_RE.search(stem) or _LEADING_EP_RE.match(stem)
    if m:
        out.add((season, int(m.group(1))))
    return out


def _is_series_dir(entry: Path) -> bool:
    """Pasta de série = tem ao menos uma subpasta 'Season NN'."""
    try:
        return any(d.is_dir() and season_of_dir(d.name) is not None
                   for d in entry.iterdir())
    except OSError:
        return False


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
# A tela do catálogo pagina em scroll infinito, então busca, ordenação e
# paginação rodam AQUI: filtrar só o que a tela já carregou deixaria a busca
# sem achar os itens das páginas seguintes. O caro de um item é varrer a
# pasta inteira (tamanho, nº de arquivos, se tem vídeo) — numa série são
# centenas de arquivos —, e o nome da pasta já basta para buscar e ordenar
# por título/ano: só as pastas da página pagam a varredura. Ordenar por
# tamanho precisa do tamanho de todas; o resultado de cada pasta fica num
# cache curto, para as páginas seguintes do mesmo scroll não refazerem tudo.
# A lista de nomes (um readdir da raiz) não é cacheada: custa quase nada e
# assim a 1ª página reflete o disco na hora, como antes da paginação.

SORT_KEYS = ("title", "year", "size")
SORT_ORDERS = ("asc", "desc")
# curto de propósito: além do mtime (abaixo), é o que limita quanto tempo uma
# mudança feita por fora (cópia pela rede dentro de "Season 01", que não muda
# o mtime da pasta da série) demora a aparecer na lista
LISTING_TTL_SECONDS = 2 * 60
# (caminho do destino, nome da pasta) -> (quando, mtime_ns da pasta, números)
_stats_cache: dict[tuple[str, str], tuple[float, int | None, dict]] = {}


def _walk_folder(path: Path) -> dict:
    """Varre a pasta de um item: tamanho total, nº de arquivos, se tem vídeo
    e se é série. Um scandir por diretório e um stat por arquivo (o rglob +
    is_file + stat de antes fazia dois stats por arquivo e mais um readdir
    para detectar a série)."""
    size = file_count = 0
    has_video = is_series = False
    stack, top = [str(path)], True
    while stack:
        try:
            it = os.scandir(stack.pop())
        except OSError:
            top = False
            continue
        with it:
            for e in it:
                try:
                    if e.is_dir():
                        # série = subpasta "Season NN" no 1º nível (layout Jellyfin)
                        if top and season_of_dir(e.name) is not None:
                            is_series = True
                        # link para pasta não é seguido (como no rglob): evita
                        # laço e contar a mesma coisa duas vezes
                        if not e.is_symlink():
                            stack.append(e.path)
                    elif e.is_file():
                        file_count += 1
                        size += e.stat().st_size
                        if os.path.splitext(e.name)[1].lower() in VIDEO_EXTENSIONS:
                            has_video = True
                except OSError:
                    continue  # arquivo sumiu no meio da varredura
        top = False
    return {"type": "series" if is_series else "movie", "size": size,
            "size_human": human_size(size), "file_count": file_count,
            "has_video": has_video}


def _folder_stats(root: str, name: str) -> dict:
    """Números da pasta, do cache quando ainda valem. O mtime da pasta muda
    quando entra/sai/renomeia arquivo direto nela (o caso comum de filme) —
    um stat só, bem mais barato que a varredura; o resto o TTL cobre."""
    path = Path(root) / name
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        mtime = None
    key = (root, name)
    hit = _stats_cache.get(key)
    now = time.monotonic()
    if hit and hit[1] == mtime and now - hit[0] <= LISTING_TTL_SECONDS:
        return hit[2]
    stats = _walk_folder(path)
    _stats_cache[key] = (now, mtime, stats)
    return stats


def _forget_stats(dest: dict, folder: str) -> None:
    """Tira uma pasta do cache da listagem (arquivo apagado/renomeado por
    aqui — numa série ele está em Season NN e o mtime da pasta não muda)."""
    _stats_cache.pop((dest["path"], Path(folder).parts[0] if folder else folder), None)


def _collate(text: str) -> str:
    """Chave de ordenação de título: sem acento e sem caixa, como o
    localeCompare('pt-BR') que a tela usava ('Ébano' junto de 'Eco')."""
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if not unicodedata.combining(c)).casefold()


def _folders(root: Path) -> list[dict]:
    """Pastas do 1º nível do destino (nome, título, ano), em ordem de nome."""
    out = []
    try:
        with os.scandir(root) as it:
            for e in it:
                try:
                    if not e.is_dir():
                        continue
                except OSError:
                    continue
                title, year = _title_and_year(e.name)
                out.append({"folder": e.name, "title": title, "year": year})
    except OSError:
        return []
    out.sort(key=lambda r: r["folder"].casefold())
    return out


def list_items(destination_id: int | None, q: str = "", sort: str = "title",
               order: str = "asc", page: int = 1,
               per_page: int | None = None) -> dict:
    """Itens do destino filtrados por `q` (título ou pasta), ordenados e
    paginados. Sem `per_page` volta tudo numa página (como a lista de jobs).

    Ordenação igual à que a tela fazia no cliente: título sem acento/caixa;
    ano com os sem-ano sempre no fim e empate por título; tamanho com empate
    na ordem das pastas. `total` conta o que casa com a busca; `total_all`,
    a coleção inteira do destino.
    """
    if sort not in SORT_KEYS:
        raise CatalogError(f"Ordenação inválida: {sort!r}")
    if order not in SORT_ORDERS:
        raise CatalogError(f"Direção inválida: {order!r}")
    dest = _resolve_dest(destination_id)
    root = Path(dest["path"])
    exists = root.is_dir()
    rows = _folders(root) if exists else []
    total_all = len(rows)

    needle = q.strip().casefold()
    if needle:
        rows = [r for r in rows
                if needle in r["title"].casefold() or needle in r["folder"].casefold()]

    desc = order == "desc"
    if sort == "title":
        # sort estável: empate de título fica na ordem das pastas
        rows.sort(key=lambda r: _collate(r["title"]), reverse=desc)
    elif sort == "year":
        def year_key(r: dict):
            y = int(r["year"]) if r["year"] else None
            return (y is None, 0 if y is None else (-y if desc else y),
                    _collate(r["title"]))
        rows.sort(key=year_key)
    else:
        # tamanho: aqui sim todas as pastas que casam são varridas (cache)
        for r in rows:
            r.update(_folder_stats(dest["path"], r["folder"]))
        rows.sort(key=lambda r: r["size"], reverse=desc)

    total = len(rows)
    if per_page is None:
        page, pages, chunk = 1, 1, rows
    else:
        page = max(1, page)
        pages = max(1, -(-total // per_page))
        chunk = rows[(page - 1) * per_page:page * per_page]
    items = [r if "size" in r else {**r, **_folder_stats(dest["path"], r["folder"])}
             for r in chunk]
    return {
        "destination": dest,
        "exists": exists,
        "items": items,
        "page": page,
        "per_page": per_page if per_page is not None else total,
        "pages": pages,
        "total": total,
        "total_all": total_all,
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


def _stat_file(path: Path) -> dict:
    """A parte BARATA do detalhe de um arquivo (só stat — sem ffprobe)."""
    info: dict = {
        "name": path.name,
        "ext": path.suffix.lower(),
        "size": 0,
        "size_human": "?",
    }
    try:
        info["size"] = path.stat().st_size
        info["size_human"] = human_size(info["size"])
    except OSError:
        pass
    is_media = path.suffix.lower() in MEDIA_EXTENSIONS
    is_sub = path.suffix.lower() in SUBTITLE_EXTENSIONS
    info["category"] = "video" if path.suffix.lower() in VIDEO_EXTENSIONS else (
        "subtitle" if is_sub else ("media" if is_media else "other"))
    return info


def _probe_file(path: Path) -> dict:
    info = _stat_file(path)

    is_media = path.suffix.lower() in MEDIA_EXTENSIONS
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
    # série NÃO sonda os arquivos aqui: 100+ episódios = 100+ ffprobes e a
    # tela levava idades para abrir — a UI pede o probe POR ARQUIVO quando a
    # linha é expandida (probe_one). Filme (poucos arquivos) segue completo.
    is_series = _is_series_dir(item_dir)
    describe = _stat_file if is_series else _probe_file
    files = []
    total = 0
    for f in sorted(item_dir.rglob("*"), key=lambda p: p.name.casefold()):
        if f.is_file():
            info = describe(f)
            info["rel"] = f.relative_to(item_dir).as_posix()
            total += info["size"]
            files.append(info)
    out = {
        "destination": dest,
        "folder": folder,
        "title": title,
        "year": year,
        "type": "series" if is_series else "movie",
        # id do TMDB já marcado no nome da pasta (None = pasta ainda não marcada)
        "tmdb_id": tmdb_id_in(folder),
        "size": total,
        "size_human": human_size(total),
        "files": files,
    }
    if is_series:
        # árvore por temporada (a UI mostra accordions): agrupa os arquivos
        # pela subpasta Season NN do rel; o que está solto na raiz vai em None
        seasons: dict[int | None, list[dict]] = {}
        for f in files:
            top = f["rel"].split("/", 1)[0] if "/" in f["rel"] else None
            season = season_of_dir(top) if top else None
            seasons.setdefault(season, []).append(f)
        out["seasons"] = [
            {"season": s, "files": fs}
            for s, fs in sorted(seasons.items(),
                                key=lambda kv: (kv[0] is None, kv[0] or 0))
        ]
    return out


def probe_one(destination_id: int | None, folder: str, rel: str) -> dict:
    """Detalhe COMPLETO (ffprobe) de um único arquivo do item — o lado caro
    que o item_detail de série deixa para quando a UI expande a linha."""
    dest = _resolve_dest(destination_id)
    root = Path(dest["path"])
    target = _safe_join(root, folder, rel)
    if not target.is_file():
        raise CatalogError("Arquivo não encontrado")
    info = _probe_file(target)
    info["rel"] = rel
    return info


# -------------------- remocao --------------------

def delete_file(destination_id: int | None, folder: str, rel: str) -> None:
    dest = _resolve_dest(destination_id)
    root = Path(dest["path"])
    target = _safe_join(root, folder, rel)
    if not target.is_file():
        raise CatalogError("Arquivo não encontrado")
    target.unlink()
    _forget_stats(dest, folder)  # tamanho/nº de arquivos da lista mudaram


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
    _forget_stats(dest, folder)  # pode ter deixado de ser (ou virado) vídeo
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


def tag_folder_with_tmdb(destination_id: int | None, folder: str, tmdb_id: int,
                         title: str | None = None, year: str | None = None) -> str:
    """Marca a pasta com [tmdbid-N] (o Jellyfin usa isso para identificar o
    filme). Se já houver um id no nome, é substituído.

    Sem `title`, mantém o título/ano da própria pasta. Com `title` (+ `year`),
    a pasta ganha o nome exato que o Outstasher daria ('Título (Ano)
    [tmdbid-N]') — a série baixada por outro programa ("Serie Exemplo/")
    passa a ser identificada pelo id no Jellyfin. Os episódios novos já caem
    nela mesmo sem renomear (existing_folder, na entrega)."""
    if title is None:
        title, year = _title_and_year(folder)
    return rename_folder(destination_id, folder, folder_name(title, year or None, tmdb_id))


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
# chave de pasta MARCADA ([tmdbid-N]) no mesmo conjunto das (título, ano): o
# "#" nunca sai do _norm_title (só [a-z0-9]), então não colide com um título
_ID_KEY = "#tmdbid"


def _norm_title(text: str) -> str:
    """Título normalizado para casar pasta com TMDB: minúsculo, sem acentos,
    só letras/números ('ROBO·X' == 'ROBOX', 'Tóquio' == 'toquio')."""
    nfkd = unicodedata.normalize("NFKD", (text or "").lower())
    stripped = "".join(c for c in nfkd if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "", stripped)


# Normalização FROUXA (2ª tentativa, só séries): pastas criadas por outros
# programas ou à mão grafam o título de outro jeito que o TMDB — dígito no
# lugar de letra (título estilizado "S3R1E" x "Serie"), "&" x "and" x nada,
# artigo inicial ("The Serie" x "Serie" x "Serie, The") e o desambiguador de
# país que alguns programas acrescentam ("Serie (US)"). Aplicada dos DOIS
# lados, então o título certo sempre casa consigo mesmo; o risco é só o de
# duas séries diferentes coincidirem depois de normalizar — por isso o ano
# (quando a pasta tem) continua valendo e o id marcado manda.
_LEET = str.maketrans("013457", "oieast")
_LOOSE_COUNTRY_RE = re.compile(r"\(\s*[A-Za-z]{2}\s*\)")
_LOOSE_AND_RE = re.compile(r"(?:\band\b|&)", re.I)
_LOOSE_ARTICLE_RE = re.compile(r"^\s*the\s+|,\s*the\s*$", re.I)


def _loose_title(text: str) -> str:
    t = _LOOSE_COUNTRY_RE.sub(" ", text or "").strip()
    t = _LOOSE_ARTICLE_RE.sub(" ", t)
    t = _LOOSE_AND_RE.sub(" ", t)
    return _norm_title(t).translate(_LEET)


def _scan_library() -> frozenset[tuple[str, str | None]]:
    """(titulo_normalizado, ano|None) de cada pasta de filme nos destinos de
    FILMES — as bibliotecas de filme e série são separadas, então uma pasta
    parecida num destino de séries não conta como "filme na coleção".

    Pasta marcada também entra pelo id ((_ID_KEY, "N")): uma pasta com o
    título numa terceira língua ("Exemplo Film (2014) [tmdbid-N]") não casa
    com o original nem com o localizado da busca, mas o id é o mesmo."""
    keys = set()
    for dest in store.list_destinations_by_media("movie"):
        root = Path(dest["path"])
        try:
            if not root.is_dir():
                continue
            for entry in root.iterdir():
                if entry.is_dir():
                    title, year = _title_and_year(entry.name)
                    if title:
                        keys.add((_norm_title(title), year))
                    tid = tmdb_id_in(entry.name)
                    if tid:
                        keys.add((_ID_KEY, str(tid)))
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
    """Marca o cache como vencido (a PRÓXIMA busca refaz o scan — nada roda já).
    Vale para os dois índices: filmes (chaves) e séries (episódios presentes),
    e para os números por pasta da listagem do catálogo."""
    _library_cache["at"] = None
    _series_cache["at"] = None
    _stats_cache.clear()


def in_library(movie: dict, keys: frozenset) -> bool:
    """O filme do TMDB já está na coleção? Casa pelo [tmdbid-N] da pasta, ou
    título original OU localizado + ano; pasta sem ano no nome casa só pelo
    título."""
    if movie.get("id") and (_ID_KEY, str(movie["id"])) in keys:
        return True
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


# -------------------- índice de episódios presentes (séries) --------------------
# Mesmo padrão do cache de coleção: TTL + invalidação junto (o
# invalidate_library acima zera os dois — quem invalida um invalida o outro).

_series_cache: dict = {"at": None, "entries": ()}


def _episodes_in_dir(entry: Path) -> dict[int, set[int]]:
    """{temporada: {episódios}} dos arquivos de vídeo da pasta da série (em
    qualquer profundidade). Dois arquivos do mesmo episódio (qualidades
    diferentes, ou merge + original) contam uma vez."""
    seasons: dict[int, set[int]] = {}
    for f in entry.rglob("*"):
        # extensão antes do is_file: não paga um stat por legenda/nfo/imagem
        if f.suffix.lower() not in VIDEO_EXTENSIONS or not f.is_file():
            continue
        hint = season_of_dir(f.parent.name) if f.parent != entry else None
        for s, e in episodes_in_filename(f.name, hint):
            seasons.setdefault(s, set()).add(e)
    return seasons


def _name_record(name: str) -> dict:
    """O que o casamento com o TMDB lê do NOME da pasta (id marcado, título
    exato e frouxo, ano) — um lugar só para o índice da coleção e para a
    escolha da pasta na entrega (existing_folder)."""
    title, year = _title_and_year(name)
    return {"folder": name, "tmdb_id": tmdb_id_in(name),
            "norm_title": _norm_title(title), "loose_title": _loose_title(title),
            "year": year}


def _scan_series() -> tuple[dict, ...]:
    """Um registro por pasta de série: {folder, destination_id,
    destination_label, tmdb_id, norm_title, loose_title, year,
    seasons: {temporada: set(episódios)}}. Só os destinos de SÉRIES entram
    (bibliotecas separadas).

    Num destino de séries, toda pasta é candidata: conta a que tem subpasta
    "Season NN" OU arquivos com episódio no nome — uma série baixada por
    outro programa pode vir sem as subpastas, ou com "Season 1" e arquivos
    "Serie - S01E01 - Titulo WEBDL-1080p.mkv"."""
    entries = []
    for dest in store.list_destinations_by_media("tv"):
        root = Path(dest["path"])
        try:
            if not root.is_dir():
                continue
            children = sorted(root.iterdir(), key=lambda p: p.name.casefold())
        except OSError:
            continue  # destino desmontado/sem permissão não derruba a busca
        for entry in children:
            try:
                # oculta (.Trash-1000, .stfolder...) não é série
                if entry.name.startswith(".") or not entry.is_dir():
                    continue
                seasons = _episodes_in_dir(entry)
                if not seasons and not _is_series_dir(entry):
                    continue
            except OSError:
                continue  # uma pasta ilegível não derruba o resto do destino
            entries.append({
                **_name_record(entry.name),
                "destination_id": dest["id"],
                "destination_label": dest["label"],
                "seasons": seasons,
            })
    return tuple(entries)


def series_index() -> tuple[dict, ...]:
    """Índice de séries na coleção (cache com o mesmo TTL da biblioteca).
    Bloqueante — chamar via asyncio.to_thread na API.

    Independente do cache de filmes: a busca de séries não consulta o de
    filmes, e amarrar um ao outro fazia todo pedido refazer o scan de séries
    enquanto ninguém tivesse buscado um filme."""
    at = _series_cache["at"]
    if at is None or time.monotonic() - at > LIBRARY_TTL_SECONDS:
        _series_cache["entries"] = _scan_series()
        _series_cache["at"] = time.monotonic()
    return _series_cache["entries"]


def match_series(tmdb_id: int | None, titles, year: str | None,
                 index: tuple[dict, ...] | None = None) -> list[tuple[dict, str]]:
    """Pastas da coleção que são esta série do TMDB: [(registro, como)], com
    `como` = "tmdbid" | "title" | "loose".

    - pasta marcada com o MESMO id: casa, sem olhar o nome;
    - pasta marcada com OUTRO id: é outra série (reboot homônimo, por
      exemplo) — com o id pedido conhecido, nunca casa pelo título;
    - sem marca: casa por qualquer um dos `titles` (original, localizado,
      inglês...) normalizado, e senão pela normalização frouxa. Ano: a pasta
      sem ano casa com qualquer um; com ano, tem que ser o do TMDB.

    Junta TODAS as pastas que casam: a mesma série pode estar em dois
    destinos, ou numa pasta do Outstasher e noutra de outro programa."""
    if isinstance(titles, str):
        titles = [titles]
    titles = [t for t in (titles or []) if t]
    norms = {n for n in map(_norm_title, titles) if n}
    looses = {n for n in map(_loose_title, titles) if n}
    year = year or None
    out = []
    for e in (series_index() if index is None else index):
        if tmdb_id and e["tmdb_id"] is not None:
            if e["tmdb_id"] == tmdb_id:
                out.append((e, "tmdbid"))
            continue
        if e["year"] is not None and e["year"] != year:
            continue
        if e["norm_title"] and e["norm_title"] in norms:
            out.append((e, "title"))
        elif e["loose_title"] and e["loose_title"] in looses:
            out.append((e, "loose"))
    return out


def series_in_library(tmdb_id: int | None, titles, year: str | None,
                      index: tuple[dict, ...] | None = None) -> dict:
    """O que a coleção tem desta série: {seasons: {temporada: [episódios]}
    (união das pastas que casam), folders: [{folder, destination_id,
    destination_label, match, episodes}]}."""
    seasons: dict[int, set[int]] = {}
    folders = []
    for e, how in match_series(tmdb_id, titles, year, index):
        for s, eps in e["seasons"].items():
            seasons.setdefault(s, set()).update(eps)
        folders.append({
            "folder": e["folder"],
            "destination_id": e["destination_id"],
            "destination_label": e["destination_label"],
            "match": how,
            "episodes": sum(len(v) for v in e["seasons"].values()),
        })
    return {"seasons": {s: sorted(eps) for s, eps in sorted(seasons.items())},
            "folders": folders}


def owned_episodes(tmdb_id: int | None, titles,
                   year: str | None) -> dict[int, list[int]]:
    """Episódios já presentes na coleção para uma série do TMDB.

    `titles`: um título ou a lista deles (original, localizado, inglês) —
    casamento em match_series. Alimenta o "na coleção" do modal de série
    ("adicionar episódios") e o selo da busca."""
    return series_in_library(tmdb_id, titles, year)["seasons"]


# -------------------- pasta já existente do título (entrega) --------------------
# A entrega montava o caminho só pelo TMDB ('Título (Ano) [tmdbid-N]'): com a
# série baixada por outro programa em "Serie Exemplo/" — ou o filme de uma
# versão antiga do Outstasher em "Filme Exemplo (2001)/", sem o id —, o
# arquivo novo nascia numa SEGUNDA pasta ao lado e o título ficava partido em
# duas no Jellyfin. A entrega agora procura no destino DO JOB uma pasta que já
# seja do título, pela mesma regra do índice da coleção (match_series), e
# entrega dentro dela. Outro destino não entra: quem escolheu o destino do job
# foi o usuário (o modal já pré-seleciona o da pasta encontrada).

# por que a pasta escolhida ganhou, do mais forte ao mais fraco (vai no evento)
_FOLDER_TIERS = (
    "nome padrão",
    "marcada com o id do TMDB",
    "nome padrão, sem o id do TMDB",
    "casou pelo título",
    "casou pelo título, com grafia aproximada",
)


def _closeness(folder: str, titles: list[str]) -> float:
    """Quão perto o título no nome da pasta está do título mais parecido do
    TMDB (0..1) — desempate entre pastas que casaram do mesmo jeito."""
    name = _collate(_title_and_year(folder)[0])
    return max((difflib.SequenceMatcher(None, name, _collate(t)).ratio()
                for t in titles), default=0.0)


def existing_folder(root, title: str, year: str | None, tmdb_id: int | None,
                    other_titles=(), require_year: bool = False) -> dict | None:
    """A pasta que JÁ existe em `root` para o título, ou None (aí a entrega
    cria a canônica, folder_name). Bloqueante (lê o destino).

    `title` é o que dá nome à pasta canônica (o original); `other_titles`
    (localizado, inglês) só ajudam a casar. Regra de match_series: mesmo
    [tmdbid-N] casa sempre, OUTRO id nunca casa pelo nome, sem marca casa
    pelo título (exato ou frouxo) e pelo ano quando a pasta tem.
    `require_year` (filmes): pasta sem ano não serve — "Filme Exemplo/" pode
    ser o original de um remake homônimo, e juntar os dois numa pasta faria o
    Jellyfin mostrar um filme só. Em série, a pasta sem ano é o normal dos
    outros programas.

    Mais de uma candidata: a canônica; senão a marcada com o mesmo id; senão
    a de nome padrão sem o id ('Título (Ano)'); senão a que casou pelo título
    (exato antes do frouxo). Empate no mesmo nível = a de nome mais próximo
    do título (e a que tem ano), registrado em `why`.

    Devolve {folder, match, canonical, why, others}: `others` são as outras
    pastas que também casaram, para o evento do job contar que o título está
    espalhado no destino."""
    year = year or None
    titles = [t for t in (title, *other_titles) if t]
    names = []
    try:
        with os.scandir(root) as it:
            for e in it:
                try:
                    # oculta (.Trash-1000, .stfolder...) nunca é pasta de título
                    if not e.name.startswith(".") and e.is_dir():
                        names.append(e.name)
                except OSError:
                    continue
    except OSError:
        return None  # destino ainda não criado / desmontado: nada a reaproveitar
    cands = match_series(tmdb_id, titles, year,
                         tuple(_name_record(n) for n in names))
    if require_year and year:
        cands = [(e, how) for e, how in cands
                 if how == "tmdbid" or e["year"] == year]
    if not cands:
        return None
    canonical = folder_name(title, year, tmdb_id)
    plain = folder_name(title, year)

    def tier(e: dict, how: str) -> int:
        if e["folder"] == canonical:
            return 0
        if how == "tmdbid":
            return 1
        if e["folder"] == plain:
            return 2
        return 3 if how == "title" else 4

    ranked = sorted(
        (tier(e, how), -_closeness(e["folder"], titles), e["year"] is None,
         e["folder"].casefold(), e["folder"], how) for e, how in cands)
    best = ranked[0]
    ties = sum(1 for r in ranked if r[0] == best[0])
    why = _FOLDER_TIERS[best[0]]
    if ties > 1:
        why += f" — {ties} pastas casam do mesmo jeito, usada a de nome mais próximo"
    return {"folder": best[4], "match": best[5], "canonical": best[0] == 0,
            "why": why, "others": [r[4] for r in ranked[1:]]}
