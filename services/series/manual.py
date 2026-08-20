"""Merge manual de série: os arquivos já estão no disco, sem torrent.

Equivalente ao "Adicionar filme" do catálogo, com o problema a mais que só
série tem: os dois lados podem NUMERAR os episódios de formas diferentes (um
com SxxEyy, outro por posição, outro com dois episódios num arquivo só). Então
aqui nada é adivinhado em silêncio:

1. `scan_side` lê uma árvore e diz, por temporada, quais arquivos existem, em
   que ORDEM eles estão (SxxEyy / absoluta / alfabética) e quanto duram —
   duração é o que denuncia arquivo fundido e par trocado;
2. `propose` cruza os dois lados com os episódios do TMDB e devolve as linhas
   já pareadas, marcando o que ficou de fora dos dois lados;
3. a UI mostra tudo isso e deixa trocar arquivo a arquivo antes de converter.

O job criado no fim tem a MESMA forma dos jobs de série normais (só sem
torrents e já com os arquivos), então alinhador, revisão, relatório e entrega
funcionam sem saber que a origem foi manual.
"""
import re
import subprocess
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import config
from services import jobs, store, transcode
from services.series import parse

VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".m2ts", ".mov", ".wmv", ".mpg",
                    ".mpeg", ".ts", ".webm"}
MAX_DEPTH = 3          # raiz da série -> temporada -> (pasta do episódio)
PROBE_WORKERS = 8      # ffprobe é I/O: paralelizar encurta muito o scan

# "Season 01", "S01", "1a Temporada", "Temporada 1", "seizoen 1"...
_SEASON_DIR_RE = re.compile(
    r"(?:^|[^a-z0-9])(?:s(?:eason)?|temporada)[ ._-]*(\d{1,2})(?:[^0-9]|$)"
    r"|(\d{1,2})[^\d\s]{0,6}[ ._-]?temporada", re.I)
# "- 137 -" / "- 137." : numeração absoluta de anime
_ABSOLUTE_RE = re.compile(r"[-\s_](\d{1,4})[-\s_.]")


class ManualError(RuntimeError):
    pass


# -------------------- leitura de uma árvore --------------------

def _season_of_dir(name: str) -> int | None:
    m = _SEASON_DIR_RE.search(name)
    if not m:
        return None
    return int(m.group(1) or m.group(2))


def _videos(root: Path) -> list[tuple[Path, int | None]]:
    """(arquivo, temporada da PASTA) de todos os vídeos sob `root`."""
    out: list[tuple[Path, int | None]] = []

    def walk(d: Path, season: int | None, depth: int):
        if depth > MAX_DEPTH:
            return
        try:
            entradas = sorted(d.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            return
        for p in entradas:
            if p.name.startswith("."):
                continue
            try:
                if p.is_dir():
                    walk(p, _season_of_dir(p.name) or season, depth + 1)
                elif (p.suffix.lower() in VIDEO_EXTENSIONS
                      and "sample" not in p.name.lower()):
                    out.append((p, season))
            except OSError:
                continue

    if root.is_file():
        return [(root, _season_of_dir(root.parent.name))]
    walk(root, _season_of_dir(root.name), 0)
    return out


def _duration(path: str) -> float | None:
    p = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", path], capture_output=True, text=True)
    try:
        return round(float(p.stdout.strip()), 1)
    except ValueError:
        return None


def scan_side(root: str, probe: bool = True) -> dict:
    """Vídeos de uma árvore, agrupados por temporada.

    A temporada de cada arquivo sai do NOME (SxxEyy) e, quando ele não diz,
    da pasta ("Season 02"). Arquivo que não revela temporada nenhuma cai em
    `unknown` — a UI deixa o usuário atribuir.
    """
    base = Path(root).expanduser()
    if not base.exists():
        raise ManualError(f"'{base}' não existe nesta máquina")
    achados = _videos(base)
    if not achados:
        raise ManualError(f"nenhum arquivo de vídeo em '{base}'")

    arquivos: list[dict] = []
    for p, season_dir in achados:
        refs = parse.parse_episode_refs(p.name)
        season = refs[0][0] if refs else season_dir
        arquivos.append({
            "path": str(p), "name": p.name,
            "dir": str(p.parent),
            "size": p.stat().st_size if p.exists() else 0,
            "season": season,
            "episodes": [e for s, e in refs if s == season] if refs else [],
            "absolute": _absolute_number(p.name) if not refs else None,
            "duration": None,
        })
    if probe:
        with ThreadPoolExecutor(max_workers=PROBE_WORKERS) as pool:
            for f, dur in zip(arquivos, pool.map(
                    _duration, [f["path"] for f in arquivos])):
                f["duration"] = dur

    por_temporada: dict[str, dict] = {}
    for f in arquivos:
        chave = str(f["season"]) if f["season"] is not None else "unknown"
        grupo = por_temporada.setdefault(chave, {"files": [], "dirs": set()})
        grupo["files"].append(f)
        grupo["dirs"].add(f["dir"])
    for chave, grupo in por_temporada.items():
        grupo["files"].sort(key=_file_sort_key)
        pastas = grupo.pop("dirs")
        # a pasta que MAIS cobre esta temporada manda: apontar a raiz para um
        # lugar que mistura séries/releases (a pasta de torrents inteira, por
        # exemplo) não pode fazer o episódio 3 vir de outra série só porque o
        # nome dela vem antes no alfabeto
        grupo["dir"] = _dominant_dir(grupo["files"]) or _common_dir(pastas)
        grupo["dirs"] = len(pastas)
        grupo["order"] = _describe_order(grupo["files"])
        grupo["episodes"] = len({e for f in grupo["files"] for e in f["episodes"]})
    return {"root": str(base), "seasons": por_temporada}


def _absolute_number(name: str) -> int | None:
    m = _ABSOLUTE_RE.search(Path(name).stem)
    return int(m.group(1)) if m else None


def _file_sort_key(f: dict):
    """Ordem natural: por episódio quando o arquivo diz, senão pelo nome."""
    ep = min(f["episodes"]) if f["episodes"] else f["absolute"]
    return (ep is None, ep if ep is not None else 0, f["name"].lower())


def _dominant_dir(files: list[dict]) -> str:
    """Pasta que cobre mais episódios distintos desta temporada."""
    cobertura: dict[str, set] = {}
    for f in files:
        alvo = cobertura.setdefault(f["dir"], set())
        alvo.update(f["episodes"] or [f["name"]])
    if not cobertura:
        return ""
    return max(cobertura.items(), key=lambda kv: (len(kv[1]), kv[0]))[0]


def _common_dir(dirs: set[str]) -> str:
    if len(dirs) == 1:
        return next(iter(dirs))
    partes = [Path(d).parts for d in dirs]
    comum: list[str] = []
    for pedaco in zip(*partes):
        if len(set(pedaco)) != 1:
            break
        comum.append(pedaco[0])
    return str(Path(*comum)) if comum else ""


def _describe_order(files: list[dict]) -> str:
    """Como este lado numera os episódios — é o que a UI mostra como 'Ordem'."""
    com_ref = [f for f in files if f["episodes"]]
    if len(com_ref) == len(files) and files:
        eps = sorted({e for f in files for e in f["episodes"]})
        fundidos = sum(1 for f in files if len(f["episodes"]) > 1)
        extra = f", {fundidos} fundido(s)" if fundidos else ""
        return f"SxxEyy (E{eps[0]:02d}–E{eps[-1]:02d}{extra})"
    absolutos = [f["absolute"] for f in files if f["absolute"] is not None]
    if len(absolutos) == len(files) and files:
        return f"absoluta ({min(absolutos)}–{max(absolutos)})"
    if com_ref:
        return f"mista ({len(com_ref)}/{len(files)} com SxxEyy)"
    return f"alfabética ({len(files)} arquivo(s))"


def scan_sides(roots: list[str], probe: bool = True) -> dict:
    """Várias pastas de um mesmo lado lidas como UM lado só.

    Um release pode estar espalhado (uma pasta por temporada, extras à parte),
    então o usuário aponta quantas quiser e elas se somam por temporada."""
    limpas = [r for r in (roots or []) if r]
    if not limpas:
        return {"root": "", "roots": [], "seasons": {}}
    partes = [scan_side(r, probe=probe) for r in limpas]
    juntas: dict[str, list[dict]] = {}
    for parte in partes:
        for chave, grupo in parte["seasons"].items():
            juntas.setdefault(chave, []).extend(grupo["files"])
    seasons: dict[str, dict] = {}
    for chave, files in juntas.items():
        vistos: dict[str, dict] = {f["path"]: f for f in files}   # sem repetir
        lista = sorted(vistos.values(), key=_file_sort_key)
        seasons[chave] = {
            "files": lista,
            "dir": _dominant_dir(lista),
            "dirs": len({f["dir"] for f in lista}),
            "order": _describe_order(lista),
            "episodes": len({e for f in lista for e in f["episodes"]}),
        }
    return {"root": limpas[0], "roots": limpas, "seasons": seasons}


def seasons_found(original: dict, dubbed: dict,
                  wanted: list[int] | None = None) -> list[int]:
    """Temporadas que os ARQUIVOS revelam (união dos dois lados).

    A tela lista as temporadas do TMDB, não estas: temporada sem arquivo
    reconhecido também precisa aparecer para receber uma pasta. Isto aqui é o
    diagnóstico de "o que as pastas dizem"."""
    achadas = {int(k) for lado in (original, dubbed) for k in lado["seasons"]
               if k != "unknown"}
    if wanted:
        achadas &= {int(s) for s in wanted}
    return sorted(achadas)


def season_group(side: dict, season: int) -> dict:
    """O grupo de arquivos de UMA temporada dentro de um scan.

    Quando a pasta escolhida não tem arquivo nenhum daquela temporada (nome
    fora do padrão, pasta "sem temporada", release que numera de outro jeito),
    a escolha do usuário MANDA: tudo o que está ali passa a valer para esta
    temporada, na ordem natural. É o que "escolher a pasta desta temporada"
    significa — senão a tela ficaria vazia sem explicação."""
    grupo = side["seasons"].get(str(season))
    if grupo and grupo["files"]:
        return grupo
    todos = [f for g in side["seasons"].values() for f in g["files"]]
    if not todos:
        return {"files": [], "dir": side.get("root", ""), "order": "—",
                "episodes": 0, "dirs": 0}
    todos.sort(key=_file_sort_key)
    return {"files": todos, "dir": _dominant_dir(todos),
            "dirs": len({f["dir"] for f in todos}),
            "order": _describe_order(todos),
            "episodes": len({e for f in todos for e in f["episodes"]})}


def scan_season(season: int, episodes: list[dict],
                original_dirs: list[str] | None,
                dubbed_dirs: list[str] | None) -> dict:
    """Uma temporada relida com as pastas escolhidas para ela.

    Os DOIS lados vêm sempre na chamada (mesmo o que não mudou): sem isso, o
    lado não informado sumiria da temporada — era o bug de "escolher a pasta
    do áudio apagava o vídeo"."""
    lados = {
        "original": scan_sides(original_dirs or []),
        "dubbed": scan_sides(dubbed_dirs or []),
    }
    montado = {
        papel: {"root": lado.get("root", ""),
                "seasons": {str(season): season_group(lado, season)}}
        for papel, lado in lados.items()
    }
    plano = propose(montado["original"], montado["dubbed"], {season: episodes})
    return {"season": plano[0], "files": {
        papel: montado[papel]["seasons"][str(season)]["files"]
        for papel in ("original", "dubbed")}}


# -------------------- pareamento --------------------

def propose(original: dict, dubbed: dict, tmdb_seasons: dict[int, list[dict]],
            ) -> list[dict]:
    """Linhas prontas por temporada: episódio do TMDB -> arquivo de cada lado.

    Casa por SxxEyy quando os DOIS lados numeram assim; senão por posição na
    ordem natural. Arquivo que cobre dois episódios entra nas duas linhas (é
    o caso fundido: o alinhador localiza a metade certa)."""
    saida: list[dict] = []
    for season in sorted(tmdb_seasons):
        eps = tmdb_seasons[season]
        lado_o = original["seasons"].get(str(season), {"files": []})
        lado_d = dubbed["seasons"].get(str(season), {"files": []})
        mapa_o = _map_files(lado_o["files"], eps, lado_o.get("dir", ""))
        mapa_d = _map_files(lado_d["files"], eps, lado_d.get("dir", ""))
        linhas = []
        for ep in eps:
            n = ep["episode"]
            o, d = mapa_o.get(n), mapa_d.get(n)
            linhas.append({
                "season": season, "episode": n, "name": ep.get("name"),
                "original": o["path"] if o else None,
                "dubbed": d["path"] if d else None,
                "orig_duration": o["duration"] if o else None,
                "dub_duration": d["duration"] if d else None,
                "include": bool(o and d),
            })
        usados_o = {ln["original"] for ln in linhas if ln["original"]}
        usados_d = {ln["dubbed"] for ln in linhas if ln["dubbed"]}
        saida.append({
            "season": season,
            "original": _side_summary(lado_o),
            "dubbed": _side_summary(lado_d),
            "rows": linhas,
            "unmatched": {
                "original": [f["path"] for f in lado_o["files"]
                             if f["path"] not in usados_o],
                "dubbed": [f["path"] for f in lado_d["files"]
                           if f["path"] not in usados_d],
            },
        })
    return saida


def _map_files(files: list[dict], eps: list[dict],
               dominant: str = "") -> dict[int, dict]:
    """episódio -> arquivo. Por SxxEyy quando o lado numera assim (arquivo
    fundido entra em cada episódio que cobre); senão por posição.

    Dois arquivos para o mesmo episódio (raiz com duas séries, ou 720p e
    1080p na mesma pasta): ganha o da pasta dominante e, dentro dela, o
    MAIOR — a mesma regra que o pipeline de torrent usa."""
    if files and all(f["episodes"] for f in files):
        mapa: dict[int, dict] = {}
        for f in files:
            for e in f["episodes"]:
                atual = mapa.get(e)
                if atual is None or _prefer(f, atual, dominant):
                    mapa[e] = f
        return mapa
    numeros = [ep["episode"] for ep in eps]
    if dominant:
        files = [f for f in files if f["dir"] == dominant] or files
    return {n: f for n, f in zip(numeros, files)}


def _prefer(novo: dict, atual: dict, dominant: str) -> bool:
    na, aa = novo["dir"] == dominant, atual["dir"] == dominant
    if na != aa:
        return na
    return (novo.get("size") or 0) > (atual.get("size") or 0)


def _side_summary(lado: dict) -> dict:
    files = lado.get("files") or []
    return {
        "dir": lado.get("dir", ""),
        "order": lado.get("order", "—"),
        "files": len(files),
        "episodes": lado.get("episodes", 0) or len(files),
        # >1 = a raiz escolhida mistura pastas (outra série, outro release):
        # a UI avisa, porque só a pasta dominante entra no pareamento
        "dirs": lado.get("dirs", 1),
    }


# -------------------- criação do job --------------------

async def create(tmdb_id: int, language: str, rows: list[dict],
                 destination_id: int | None = None,
                 convert: dict | None = None) -> dict:
    """Job de série a partir de pares de arquivos já no disco.

    Mesma forma dos jobs de série (episódios com subestado, relatório, regra
    dos 75%), mas sem torrents e já em `merging`: o pipeline pesado começa no
    merge/alinhamento."""
    from services import tmdb as tmdb_api
    from services.series import merge_runner

    if language not in config.LANGUAGES:
        raise ValueError(f"Idioma inválido: {language!r}")
    if convert is not None:
        convert = transcode.validate(convert).to_dict()
    pares = _validate_rows(rows)

    dest = store.get_destination(destination_id) if destination_id else None
    if dest is not None and dest.get("media", "movie") != "tv":
        raise ValueError(
            f"O destino '{dest['label']}' é da biblioteca de FILMES — séries "
            f"vão para um destino de séries (Configurações → Destinos)")
    dest = dest or store.default_destination("tv")
    if dest is None:
        raise ValueError("Nenhum destino de SÉRIES cadastrado — crie um em "
                         "Configurações → Destinos (mídia: Séries)")

    info = await tmdb_api.tv_details(tmdb_id, language)
    nomes = {}
    for season in sorted({p["season"] for p in pares}):
        try:
            data = await tmdb_api.tv_season(tmdb_id, season)
        except Exception:  # noqa: BLE001 — sem o nome do episódio dá para viver
            continue
        for ep in data["episodes"]:
            nomes[(season, ep["episode"])] = ep

    episodes = {}
    for p in pares:
        chave = f"S{p['season']:02d}E{p['episode']:02d}"
        meta = nomes.get((p["season"], p["episode"]), {})
        episodes[chave] = {
            "season": p["season"], "episode": p["episode"],
            "name": meta.get("name"), "air_date": meta.get("air_date"),
            "runtime": meta.get("runtime"),
            "state": "downloaded",       # os arquivos já estão aqui
            "src": {"original": p["original"], "dubbed": p["dubbed"]},
            "output": None, "error": None,
        }

    job = {
        "id": uuid.uuid4().hex[:10],
        "media_type": "tv",
        "tmdb_id": tmdb_id,
        "language": language,
        "mode": "files",             # sem busca, sem qBittorrent
        "kind": "series",
        "download_only": False,
        "convert": convert,
        "status": "merging",
        "detail": f"{len(episodes)} episódio(s) para converter...",
        "movie": {k: info[k] for k in
                  ("id", "original_title", "localized_title", "english_title",
                   "original_language", "year", "overview", "poster")},
        "request": {"seasons": [], "episodes": {}},
        "episodes": episodes,
        "torrents": [],
        "awaiting": None,
        "order_map": None,
        "report": None,
        "progress": {},
        "output": None,
        "search_tv": None,
        "manual_rows": pares,        # para o ↻ recriar o mesmo job
        "destination_id": dest["id"], "destination_label": dest["label"],
        "destination_path": dest["path"],
        "torrent_target_id": None, "torrent_target_label": None,
        "torrent_save_path": "", "torrent_local_path": "",
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    jobs._jobs[job["id"]] = job
    conv = transcode.describe(convert)
    cinfo = f" — conversão: {', '.join(conv)}" if conv else ""
    jobs._event(job, "status",
                f"Merge manual de série: {len(episodes)} episódio(s) — destino: "
                f"{dest['label']}{cinfo}")
    jobs._spawn(job["id"], merge_runner.merge_all(job))
    return jobs._public(job)


def _validate_rows(rows: list[dict]) -> list[dict]:
    """Pares válidos e sem repetição de episódio. Falha ANTES de criar o job:
    caminho inexistente aqui é erro de digitação, não de conversão."""
    vistos: set[tuple[int, int]] = set()
    saida: list[dict] = []
    for r in rows or []:
        if not r.get("include", True):
            continue
        try:
            season, episode = int(r["season"]), int(r["episode"])
        except (KeyError, TypeError, ValueError):
            raise ValueError("linha sem temporada/episódio")
        o, d = r.get("original"), r.get("dubbed")
        if not o or not d:
            raise ValueError(f"S{season:02d}E{episode:02d}: falta o arquivo "
                             f"{'original' if not o else 'dublado'}")
        for papel, caminho in (("original", o), ("dublado", d)):
            if not Path(caminho).is_file():
                raise ValueError(f"S{season:02d}E{episode:02d}: arquivo "
                                 f"{papel} não existe ({caminho})")
        if (season, episode) in vistos:
            raise ValueError(f"S{season:02d}E{episode:02d} aparece duas vezes")
        vistos.add((season, episode))
        saida.append({"season": season, "episode": episode,
                      "original": str(o), "dubbed": str(d)})
    if not saida:
        raise ValueError("Nenhum episódio selecionado")
    return sorted(saida, key=lambda p: (p["season"], p["episode"]))
