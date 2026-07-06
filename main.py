"""API do servico de download + merge.

Rode com:
    python main.py        # produção: builda o frontend (se mudou) e serve tudo em :8008
    python main.py dev    # dev: API em :8008 com reload + Vite em watch em :5173
"""
import shutil
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

import config
from services import catalog, jobs, store, tmdb

FRONTEND_DIR = Path(__file__).parent / "frontend"
DIST_DIR = FRONTEND_DIR / "dist"


def _disk_for(path: str) -> dict | None:
    """Uso do disco que contém `path`, na visão DESTA máquina.

    Sobe pelos diretórios até achar um que exista (o destino pode ainda não ter
    sido criado). Retorna None se nada no caminho existir ou o SO recusar.
    """
    p = Path(path)
    for candidate in (p, *p.parents):
        if candidate.exists():
            try:
                total, used, free = shutil.disk_usage(candidate)
            except OSError:
                return None
            return {"total": total, "used": used, "free": free}
    return None


def _with_disk(dest: dict) -> dict:
    return {**dest, "disk": _disk_for(dest["path"])}


@asynccontextmanager
async def lifespan(app: FastAPI):
    jobs.load()
    jobs.resume_pending()
    yield


app = FastAPI(title="Movie Downloader & Merger", lifespan=lifespan)


@app.get("/api/languages")
async def languages():
    return [{"code": code, "label": info["label"]} for code, info in config.LANGUAGES.items()]


@app.get("/api/movies")
async def movies(q: str = ""):
    if not config.TMDB_API_KEY:
        raise HTTPException(500, "TMDB_API_KEY não configurada no .env")
    if q.strip():
        return await tmdb.search(q.strip())
    return await tmdb.popular()


class JobRequest(BaseModel):
    tmdb_id: int
    language: str
    mode: str = "auto"  # auto | manual
    destination_id: int | None = None
    torrent_target_id: int | None = None


class SelectRequest(BaseModel):
    audio_id: str
    video_id: str


class CancelRequest(BaseModel):
    delete_torrents: bool = False


class DestinationRequest(BaseModel):
    label: str
    path: str
    is_default: bool = False


class TorrentTargetRequest(BaseModel):
    label: str
    save_path: str = ""
    local_path: str = ""
    is_default: bool = False


# -------------------- destinos --------------------

@app.get("/api/destinations")
async def list_destinations():
    return [_with_disk(d) for d in store.list_destinations()]


@app.post("/api/destinations")
async def add_destination(req: DestinationRequest):
    if not req.label.strip() or not req.path.strip():
        raise HTTPException(400, "Nome e caminho são obrigatórios")
    return _with_disk(store.add_destination(req.label.strip(), req.path.strip(), req.is_default))


@app.put("/api/destinations/{dest_id}")
async def update_destination(dest_id: int, req: DestinationRequest):
    if not req.label.strip() or not req.path.strip():
        raise HTTPException(400, "Nome e caminho são obrigatórios")
    dest = store.update_destination(dest_id, req.label.strip(), req.path.strip(), req.is_default)
    if not dest:
        raise HTTPException(404, "Destino não encontrado")
    return _with_disk(dest)


@app.delete("/api/destinations/{dest_id}")
async def delete_destination(dest_id: int):
    if not store.delete_destination(dest_id):
        raise HTTPException(404, "Destino não encontrado")
    return {"ok": True}


# -------------------- destinos dos torrents (qBittorrent) --------------------

def _target_with_disk(t: dict) -> dict:
    # o disco relevante e o do caminho local (onde os torrents caem NESTA maquina)
    return {**t, "disk": _disk_for(t["local_path"]) if t.get("local_path") else None}


@app.get("/api/torrent-targets")
async def list_torrent_targets():
    return [_target_with_disk(t) for t in store.list_torrent_targets()]


@app.post("/api/torrent-targets")
async def add_torrent_target(req: TorrentTargetRequest):
    if not req.label.strip():
        raise HTTPException(400, "Nome é obrigatório")
    return _target_with_disk(store.add_torrent_target(
        req.label.strip(), req.save_path.strip(), req.local_path.strip(), req.is_default))


@app.put("/api/torrent-targets/{target_id}")
async def update_torrent_target(target_id: int, req: TorrentTargetRequest):
    if not req.label.strip():
        raise HTTPException(400, "Nome é obrigatório")
    target = store.update_torrent_target(
        target_id, req.label.strip(), req.save_path.strip(), req.local_path.strip(),
        req.is_default)
    if not target:
        raise HTTPException(404, "Destino de torrents não encontrado")
    return _target_with_disk(target)


@app.delete("/api/torrent-targets/{target_id}")
async def delete_torrent_target(target_id: int):
    if not store.delete_torrent_target(target_id):
        raise HTTPException(404, "Destino de torrents não encontrado")
    return {"ok": True}


# -------------------- catalogo --------------------

@app.get("/api/catalog")
async def catalog_list(destination_id: int | None = None):
    try:
        return catalog.list_items(destination_id)
    except catalog.CatalogError as e:
        raise HTTPException(400, str(e))


@app.get("/api/catalog/item")
async def catalog_item(folder: str, destination_id: int | None = None):
    try:
        detail = catalog.item_detail(destination_id, folder)
    except catalog.CatalogError as e:
        raise HTTPException(404, str(e))
    # match no TMDB (falha de rede nao quebra a pagina)
    try:
        detail["tmdb"] = await tmdb.match(detail["title"], detail["year"])
    except Exception:  # noqa: BLE001
        detail["tmdb"] = None
    return detail


@app.delete("/api/catalog/file")
async def catalog_delete_file(folder: str, rel: str, destination_id: int | None = None):
    try:
        catalog.delete_file(destination_id, folder, rel)
    except catalog.CatalogError as e:
        raise HTTPException(404, str(e))
    return {"ok": True}


@app.delete("/api/catalog/item")
async def catalog_delete_folder(folder: str, destination_id: int | None = None):
    try:
        catalog.delete_folder(destination_id, folder)
    except catalog.CatalogError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.post("/api/jobs")
async def create_job(req: JobRequest):
    if req.language not in config.LANGUAGES:
        raise HTTPException(400, f"Idioma inválido: {req.language}")
    if req.mode not in ("auto", "manual"):
        raise HTTPException(400, f"Modo inválido: {req.mode}")
    try:
        return await jobs.create(req.tmdb_id, req.language, req.mode,
                                 req.destination_id, req.torrent_target_id)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/jobs")
async def list_jobs():
    return jobs.list_jobs()


@app.get("/api/jobs/{job_id}")
async def job_detail(job_id: str):
    job = jobs.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job não encontrado")
    return job


@app.post("/api/jobs/{job_id}/select")
async def select_job(job_id: str, req: SelectRequest):
    try:
        job = await jobs.select(job_id, req.audio_id, req.video_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not job:
        raise HTTPException(409, "Job não está aguardando escolha")
    return job


@app.post("/api/jobs/{job_id}/cancel")
async def cancel_job(job_id: str, req: CancelRequest):
    job = await jobs.cancel(job_id, req.delete_torrents)
    if not job:
        raise HTTPException(404, "Job não encontrado")
    return {"ok": True}


@app.post("/api/jobs/{job_id}/retry")
async def retry_job(job_id: str):
    job = await jobs.retry(job_id)
    if not job:
        raise HTTPException(409, "Só é possível repetir jobs com erro ou cancelados")
    return job


@app.delete("/api/jobs/{job_id}")
async def delete_job(job_id: str, delete_torrents: bool = False):
    if not await jobs.remove(job_id, delete_torrents):
        raise HTTPException(404, "Job não encontrado")
    return {"ok": True}


# -------------------- SPA (React buildado em frontend/dist) --------------------
# Registrado por ultimo: as rotas /api acima tem prioridade.

@app.get("/{full_path:path}")
async def spa(full_path: str):
    index = DIST_DIR / "index.html"
    if not index.exists():
        raise HTTPException(
            503, "Frontend não buildado — rode 'python main.py' (builda sozinho) "
                 "ou 'npm run build' dentro de frontend/")
    if full_path:
        candidate = (DIST_DIR / full_path).resolve()
        if candidate.is_file() and candidate.is_relative_to(DIST_DIR.resolve()):
            return FileResponse(candidate)
    return FileResponse(index)  # rotas do react-router (ex.: /jobs/abc) caem no index


# -------------------- build/dev do frontend --------------------

def _npm() -> str | None:
    return shutil.which("npm")


def _dist_stale() -> bool:
    index = DIST_DIR / "index.html"
    if not index.exists():
        return True
    built = index.stat().st_mtime
    sources = [FRONTEND_DIR / "index.html", FRONTEND_DIR / "vite.config.ts",
               FRONTEND_DIR / "package.json",
               *(FRONTEND_DIR / "src").rglob("*")]
    return any(f.is_file() and f.stat().st_mtime > built for f in sources)


def _ensure_frontend_built():
    npm = _npm()
    if not npm:
        if (DIST_DIR / "index.html").exists():
            print("AVISO: npm não encontrado — servindo o build existente de frontend/dist")
        else:
            print("AVISO: npm não encontrado e frontend/dist não existe — só a API vai funcionar")
        return
    if not (FRONTEND_DIR / "node_modules").exists():
        print("Instalando dependências do frontend (npm install)...")
        subprocess.run([npm, "install", "--no-fund", "--no-audit"], cwd=FRONTEND_DIR, check=True)
    if _dist_stale():
        print("Buildando o frontend (npm run build)...")
        subprocess.run([npm, "run", "build"], cwd=FRONTEND_DIR, check=True)
    else:
        print("Frontend já está buildado e atualizado.")


if __name__ == "__main__":
    import uvicorn

    if "dev" in sys.argv[1:]:
        npm = _npm()
        vite = None
        if npm:
            if not (FRONTEND_DIR / "node_modules").exists():
                subprocess.run([npm, "install", "--no-fund", "--no-audit"],
                               cwd=FRONTEND_DIR, check=True)
            vite = subprocess.Popen([npm, "run", "dev"], cwd=FRONTEND_DIR)
            print("\nFrontend (watch): http://127.0.0.1:5173  |  API: http://127.0.0.1:8008\n")
        else:
            print("AVISO: npm não encontrado — modo dev só com a API")
        try:
            uvicorn.run("main:app", host="127.0.0.1", port=8008, reload=True)
        finally:
            if vite:
                vite.terminate()
    else:
        _ensure_frontend_built()
        uvicorn.run(app, host="127.0.0.1", port=8008)
