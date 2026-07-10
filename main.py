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

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

import config
from services import auth, catalog, jackett, jobs, store, tmdb

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


app = FastAPI(title="Outstasher", lifespan=lifespan)


# -------------------- autenticacao --------------------
# Rotas de auth que NAO exigem sessao (status/setup/login). O resto de /api/* e
# protegido pelo middleware abaixo; arquivos estaticos do SPA ficam liberados
# (a propria tela de login e servida por eles).
_PUBLIC_PATHS = {"/api/auth/status", "/api/auth/setup", "/api/auth/login"}


def _bearer(request: Request) -> str | None:
    h = request.headers.get("Authorization", "")
    return h[7:].strip() if h.lower().startswith("bearer ") else None


@app.middleware("http")
async def auth_gate(request: Request, call_next):
    path = request.url.path
    # so protege a API; o SPA (html/js/css) e servido livremente
    if path.startswith("/api/") and path not in _PUBLIC_PATHS:
        # sem senha cadastrada ainda: obriga o setup antes de qualquer coisa
        if not auth.is_password_set():
            return JSONResponse({"detail": "Senha não configurada"}, status_code=401)
        if not auth.validate_token(_bearer(request)):
            return JSONResponse({"detail": "Não autenticado"}, status_code=401)
    return await call_next(request)


class PasswordRequest(BaseModel):
    password: str


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


@app.get("/api/auth/status")
async def auth_status(request: Request):
    """A UI chama isto no boot: precisa de setup? esta logado?"""
    return {
        "password_set": auth.is_password_set(),
        "authenticated": auth.validate_token(_bearer(request)),
    }


@app.post("/api/auth/setup")
async def auth_setup(req: PasswordRequest):
    if auth.is_password_set():
        raise HTTPException(409, "Senha já configurada — use o login")
    if len(req.password) < 4:
        raise HTTPException(400, "A senha precisa ter pelo menos 4 caracteres")
    auth.set_password(req.password)
    return {"token": auth.create_session()}


@app.post("/api/auth/login")
async def auth_login(req: PasswordRequest):
    if not auth.is_password_set():
        raise HTTPException(409, "Nenhuma senha configurada ainda")
    if not auth.verify_password(req.password):
        raise HTTPException(401, "Senha incorreta")
    return {"token": auth.create_session()}


@app.post("/api/auth/logout")
async def auth_logout(request: Request):
    auth.revoke_session(_bearer(request))
    return {"ok": True}


@app.post("/api/auth/change-password")
async def auth_change_password(req: ChangePasswordRequest):
    if not auth.verify_password(req.current_password):
        raise HTTPException(401, "Senha atual incorreta")
    if len(req.new_password) < 4:
        raise HTTPException(400, "A nova senha precisa ter pelo menos 4 caracteres")
    auth.set_password(req.new_password)
    auth.revoke_all_sessions()  # derruba as outras sessoes
    return {"token": auth.create_session()}  # mantem quem trocou logado


@app.get("/api/languages")
async def languages():
    return [{"code": code, "label": info["label"]} for code, info in config.LANGUAGES.items()]


# -------------------- cadastro de idiomas (dublagem) --------------------

@app.get("/api/language-config")
async def get_language_config():
    """Idiomas cadastrados + marcadores de legenda, para a tela de edição."""
    return {
        "languages": store.list_languages(),
        "subtitle_markers": store.get_subtitle_markers(),
    }


class LanguageIn(BaseModel):
    code: str
    label: str
    tmdb: str
    markers_strong: list[str] = []
    markers_weak: list[str] = []


class LanguageConfigRequest(BaseModel):
    languages: list[LanguageIn]
    subtitle_markers: list[str] = []


def _norm_markers(markers: list[str]) -> list[str]:
    """Limpa marcadores: minúsculas, sem espaços nas pontas, sem vazios/duplicados."""
    out, seen = [], set()
    for m in markers:
        m = (m or "").strip().lower()
        if m and m not in seen:
            seen.add(m)
            out.append(m)
    return out


@app.put("/api/language-config")
async def put_language_config(req: LanguageConfigRequest):
    if not req.languages:
        raise HTTPException(400, "Cadastre ao menos um idioma")
    seen_codes = set()
    langs = []
    for lg in req.languages:
        code = lg.code.strip().lower()
        if not code:
            raise HTTPException(400, "Código de idioma vazio")
        if not code.isalnum():
            raise HTTPException(400, f"Código inválido '{lg.code}' — use só letras/números (ex.: pt)")
        if code in seen_codes:
            raise HTTPException(400, f"Código duplicado: {code}")
        seen_codes.add(code)
        if not lg.label.strip():
            raise HTTPException(400, f"Nome vazio para o idioma '{code}'")
        if not lg.tmdb.strip():
            raise HTTPException(400, f"Código TMDB vazio para o idioma '{code}'")
        langs.append({
            "code": code,
            "label": lg.label.strip(),
            "tmdb": lg.tmdb.strip(),
            "markers_strong": _norm_markers(lg.markers_strong),
            "markers_weak": _norm_markers(lg.markers_weak),
        })
    store.save_languages(langs, _norm_markers(req.subtitle_markers))
    return {"ok": True, "languages": store.list_languages(),
            "subtitle_markers": store.get_subtitle_markers()}


@app.get("/api/movies")
async def movies(q: str = "", page: int = 1):
    if not config.TMDB_API_KEY:
        raise HTTPException(500, "TMDB_API_KEY não configurada no .env")
    page = max(1, min(page, 500))  # TMDB aceita no máximo 500 páginas
    return await (tmdb.search(q.strip(), page) if q.strip() else tmdb.popular(page))


class JobRequest(BaseModel):
    tmdb_id: int
    language: str
    mode: str = "auto"  # auto | manual
    kind: str = "both"  # both (merge) | original | dubbed
    destination_id: int | None = None
    torrent_target_id: int | None = None


class SelectRequest(BaseModel):
    audio_id: str | None = None
    video_id: str | None = None


class SwitchRequest(BaseModel):
    kind: str  # video | audio
    candidate_id: str | None = None  # vazio = "Tentar próximo" (primeiro reserva)


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


# -------------------- buscas extras (idioma x variante x indexers) --------------------

@app.get("/api/jackett/indexers")
async def jackett_indexers():
    """Indexers configurados no Jackett, para montar as regras de busca extra."""
    try:
        return await jackett.list_indexers(configured_only=True)
    except Exception as e:  # noqa: BLE001 - Jackett fora do ar não deve dar 500 feio
        raise HTTPException(502, f"Falha ao listar indexers do Jackett: {e}")


@app.get("/api/extra-search-rules")
async def get_extra_search_rules():
    return {
        "rules": store.get_extra_search_rules(),
        "variants": list(store.EXTRA_SEARCH_VARIANTS),
        "languages": [{"code": c, "label": v["label"]}
                      for c, v in config.LANGUAGES.items()],
    }


class ExtraSearchRulesRequest(BaseModel):
    rules: dict


@app.put("/api/extra-search-rules")
async def put_extra_search_rules(req: ExtraSearchRulesRequest):
    # sanitiza: só idiomas conhecidos, só variantes válidas, indexers como strings
    clean: dict = {}
    for lang, variants in (req.rules or {}).items():
        if lang not in config.LANGUAGES or not isinstance(variants, dict):
            continue
        lang_rules: dict = {}
        for variant, indexers in variants.items():
            if variant not in store.EXTRA_SEARCH_VARIANTS or not isinstance(indexers, list):
                continue
            ids = [str(i) for i in indexers if i]
            if ids:
                lang_rules[variant] = ids
        if lang_rules:
            clean[lang] = lang_rules
    store.set_extra_search_rules(clean)
    return {"ok": True, "rules": clean}


# -------------------- catalogo --------------------

@app.get("/api/catalog")
async def catalog_list(destination_id: int | None = None):
    try:
        result = catalog.list_items(destination_id)
    except catalog.CatalogError as e:
        raise HTTPException(400, str(e))
    result["destination"] = _with_disk(result["destination"])
    return result


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
    if req.kind not in jobs.KINDS:
        raise HTTPException(400, f"Tipo inválido: {req.kind}")
    try:
        return await jobs.create(req.tmdb_id, req.language, req.mode,
                                 req.destination_id, req.torrent_target_id, req.kind)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/jobs")
async def list_jobs():
    return jobs.list_jobs()


# --- rotas enxutas para polling granular (declaradas ANTES de /{job_id}) ---

@app.get("/api/jobs/summary")
async def jobs_summary():
    """Processos em andamento + erros (mínimo) para o dropdown do cabeçalho."""
    return jobs.summary()


@app.get("/api/jobs/counts")
async def jobs_counts():
    """Contagem por grupo (active/error/done/all) para os badges do filtro."""
    return jobs.counts()


@app.get("/api/jobs/list")
async def jobs_list(group: str = "active"):
    """Cards enxutos da tela de Downloads, filtrados por grupo no backend."""
    try:
        return jobs.list_group(group)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/jobs/{job_id}")
async def job_detail(job_id: str):
    job = jobs.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job não encontrado")
    return job


@app.get("/api/jobs/{job_id}/progress")
async def job_progress(job_id: str):
    """Só status/detail/progresso — tick de 1s do detalhe do job."""
    p = jobs.progress(job_id)
    if not p:
        raise HTTPException(404, "Job não encontrado")
    return p


@app.post("/api/jobs/{job_id}/select")
async def select_job(job_id: str, req: SelectRequest):
    try:
        job = await jobs.select(job_id, req.audio_id, req.video_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not job:
        raise HTTPException(409, "Job não está aguardando escolha")
    return job


@app.post("/api/jobs/{job_id}/proceed")
async def proceed_job(job_id: str):
    """Continua a conversão pausada por suspeita de versão/corte diferente."""
    job = await jobs.proceed(job_id)
    if not job:
        raise HTTPException(409, "Job não está aguardando confirmação de conversão")
    return job


@app.post("/api/jobs/{job_id}/switch")
async def switch_job(job_id: str, req: SwitchRequest):
    """Troca o torrent de um download em andamento (próximo reserva ou escolhido)."""
    if req.kind not in ("video", "audio"):
        raise HTTPException(400, f"kind inválido: {req.kind}")
    try:
        job = await jobs.switch(job_id, req.kind, req.candidate_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:  # qBittorrent fora do ar etc.
        raise HTTPException(502, f"Falha ao trocar o torrent: {e}")
    if not job:
        raise HTTPException(404, "Job não encontrado")
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
        # HOST/PORT configuraveis para o container (bind em 0.0.0.0). Local, o
        # padrao continua 127.0.0.1 para nao expor o servico sem querer.
        import os
        host = os.getenv("HOST", "127.0.0.1")
        port = int(os.getenv("PORT", "8008"))
        uvicorn.run(app, host=host, port=port)
