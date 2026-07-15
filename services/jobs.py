"""Orquestrador: TMDB -> Jackett -> qBittorrent -> merge interno.

Cada job guarda um log de eventos estruturado ({ts, kind, message, data?})
persistido em jobs.json — o frontend consome isso pela lupa (detalhe do job).

Modos:
- auto: escolhe os torrents sozinho (áudio define o corte; vídeo tem que casar).
- manual: para em "awaiting" com os candidatos viáveis; o usuário escolhe
  pela UI e o job continua via select().

Watchdog: download sem progresso é trocado pelo próximo candidato viável do
mesmo corte (job["fallbacks"]). O timeout depende do estado do torrent:
metaDL espera mais (30 min) e stalledDL muito mais (2 h). Perder a conexão
com o qBittorrent nunca falha o job — ele avisa e fica tentando reconectar;
torrent que sumiu do qBittorrent é reinserido automaticamente.
"""
import asyncio
import re
import time
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote_plus

import httpx

import config
from services import catalog, jackett, merger, selector, store, tmdb, transcode
from services.qbittorrent import QbitClient, QbitError

# problemas de comunicação com o qBittorrent que NÃO devem falhar o job
# durante o download: rede fora, sessão caída, restart do qBittorrent...
_CONN_ERRORS = (httpx.HTTPError, QbitError, OSError)

VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".m2ts", ".mov", ".wmv", ".mpg", ".mpeg"}
MAX_SELECTABLE = 60  # candidatos guardados por papel para selecao manual/fallback

# estados: searching -> (awaiting ->) downloading -> merging -> done | error | cancelled
# merging pode voltar a awaiting: se a validação do offset em duas janelas
# divergir (possível versão/corte diferente), o job para com `drift_confirm` e
# espera o usuário mandar Continuar (proceed) em vez de gastar o re-encode.
# TERMINAL: já são só histórico (não precisam de acesso rápido). Ficam SÓ no
# banco; não ocupam memória. ACTIVE: em andamento — precisam de polling de
# progresso e ações rápidas, então vivem em _jobs (espelhados no banco).
_TERMINAL_STATUSES = ("done", "error", "cancelled")
_ACTIVE_STATUSES = ("searching", "awaiting", "downloading", "merging")
# _jobs guarda APENAS os jobs ativos. Terminais são lidos do banco sob demanda.
_jobs: dict[str, dict] = {}
_tasks: dict[str, asyncio.Task] = {}
_qbit = QbitClient()

# fila de conversão: só 1 merge/entrega roda por vez (ffmpeg é pesado de CPU/IO).
# lazy porque o event loop pode não existir no import.
_merge_lock: asyncio.Lock | None = None


def _get_merge_lock() -> asyncio.Lock:
    global _merge_lock
    if _merge_lock is None:
        _merge_lock = asyncio.Lock()
    return _merge_lock


def _spawn(job_id: str, coro):
    """Cria a task do pipeline e garante que ela sai de _tasks ao terminar
    (senão Tasks concluídas vazariam para sempre, como os jobs vazavam)."""
    task = asyncio.create_task(coro)
    _tasks[job_id] = task
    task.add_done_callback(
        lambda t: _tasks.pop(job_id, None) if _tasks.get(job_id) is t else None)
    return task


def load():
    store.init()
    # só os ativos entram em memória; terminais ficam no banco (histórico)
    for job in store.load_jobs_by_status(_ACTIVE_STATUSES):
        _jobs[job["id"]] = job


def resume_pending():
    """Retoma jobs interrompidos por um restart do servidor."""
    # cópia: _set(..., "error") remove o job de _jobs no meio da iteração
    for job in list(_jobs.values()):
        if job.get("manual_files"):
            _resume_manual(job)
        elif job["status"] in ("downloading", "merging"):
            job["status"] = "downloading"
            _event(job, "status", "Servidor reiniciado — retomando acompanhamento dos downloads")
            _spawn(job["id"], _run_from_download(job))
        elif job["status"] == "searching":
            _set(job, "error", "Servidor reiniciado durante a busca — use ↻ para tentar de novo")
        # awaiting: candidatos estao persistidos; segue esperando a escolha


def _resume_manual(job: dict):
    """Retoma uma conversão manual interrompida por restart: os arquivos de
    origem estão no disco (não dependem do qBittorrent), então é só recomeçar
    o merge do zero. A pausa de drift (awaiting) segue esperando a decisão."""
    if job["status"] == "awaiting":
        return
    info = job["manual_files"]
    vf, af = Path(info["video"]), Path(info["audio"])
    if vf.is_file() and af.is_file():
        _event(job, "status", "Servidor reiniciado — recomeçando a conversão manual")
        _spawn(job["id"], _run_manual(job, vf, af))
    else:
        _set(job, "error", "Servidor reiniciado e os arquivos de origem não existem mais "
                           "— crie a conversão de novo")


def _public(job: dict) -> dict:
    return {k: v for k, v in job.items() if k not in ("events", "search")}


def _lookup(job_id: str) -> dict | None:
    """Job ativo (memória) ou terminal (banco). Para leitura/ações que também
    valem em histórico (ver detalhe, retry, remover). O dict do banco é uma
    cópia — mutar não afeta memória (correto: terminais são imutáveis)."""
    return _jobs.get(job_id) or store.get_job(job_id)


def list_jobs() -> list[dict]:
    """Lista leve (sem eventos nem candidatos) para o polling da pagina.

    Junta os ativos (memória, com progresso ao vivo) com os terminais (banco,
    histórico) e ordena por data de criação.
    """
    terminal = store.load_jobs_by_status(_TERMINAL_STATUSES)
    combined = list(_jobs.values()) + terminal
    ordered = sorted(combined, key=lambda j: j["created_at"], reverse=True)
    return [_public(j) for j in ordered]


def get_job(job_id: str) -> dict | None:
    """Job completo: eventos (para a lupa) + candidatos (para a escolha manual).

    Ativo vem da memória (progresso ao vivo); terminal vem do banco.
    """
    job = _lookup(job_id)
    if not job:
        return None
    return {**_public(job), "events": store.load_events(job_id), "search": job.get("search")}


# -------------------- leituras enxutas (polling granular) --------------------
# O frontend faz polling em ritmos diferentes por tela; cada rota traz SÓ o
# mínimo que aquela tela renderiza, em vez da lista completa de jobs.

def _pct(p) -> float | None:
    """Extrai o percentual (0-100) de um valor de progresso (objeto ou número)."""
    if p is None:
        return None
    if isinstance(p, (int, float)):
        return float(p)
    if isinstance(p, dict):
        return p.get("pct")
    return None


# status -> estado visual do filme/processo (menor rank = maior prioridade).
# cancelled não vira estado (some da UI). done/error contam como histórico.
_STATE_OF = {"merging": "converting", "downloading": "downloading",
             "searching": "searching", "awaiting": "awaiting",
             "done": "done", "error": "error"}
_STATE_RANK = {"converting": 0, "downloading": 1, "searching": 2,
               "awaiting": 3, "done": 4, "error": 5}


def _all_jobs() -> list[dict]:
    """Ativos (memória) + terminais (banco), sem duplicar."""
    return list(_jobs.values()) + store.load_jobs_by_status(_TERMINAL_STATUSES)


def _movie_title(job: dict) -> str:
    m = job.get("movie")
    if m and m.get("original_title"):
        return f"{m['original_title']} ({m.get('year', '')})".strip()
    return f"TMDB #{job.get('tmdb_id')}"


def _download_pct(job: dict) -> float | None:
    """Progresso do DOWNLOAD do job, 0-100, como média dos torrents que ele baixa.

    Um job dublado baixa dois torrents (vídeo original + áudio dublado) e só
    termina quando os DOIS chegam a 100%, então cada um vale 50%. Torrent ainda
    sem leitura do qBittorrent conta como 0% — o denominador é o que o job
    PRECISA baixar, não o que já reportou (senão o vídeo sozinho em 40% viraria
    "40% do job", e a barra andaria para trás quando o áudio aparecesse).
    """
    needed = _needed_torrents(job)
    read = [_pct(job["progress"].get(k)) for k in needed]
    if not any(p is not None for p in read):
        return None  # nenhum torrent reportou ainda (searching/awaiting): sem barra
    return sum(p or 0.0 for p in read) / len(read)


def summary() -> list[dict]:
    """Lista mínima de processos EM ANDAMENTO + erros, para o dropdown do
    cabeçalho. Só o essencial para o item da lista (sem candidatos/eventos)."""
    out = []
    for j in _all_jobs():
        state = _STATE_OF.get(j["status"])
        if state in (None, "done"):  # dropdown ignora concluídos e cancelados
            continue
        pct = ((j["progress"].get("merge") or {}).get("pct") if state == "converting"
               else _download_pct(j))
        out.append({"id": j["id"], "tmdb_id": j.get("tmdb_id"),
                    "title": _movie_title(j), "status": j["status"],
                    "state": state, "pct": pct})
    out.sort(key=lambda x: _STATE_RANK[x["state"]])
    return out


# grupos de status expostos no filtro da tela de Downloads
_GROUPS = {
    "active": ("searching", "awaiting", "downloading", "merging"),
    "error": ("error", "cancelled"),
    "done": ("done",),
}


def counts() -> dict[str, int]:
    """Contagem por grupo (active/error/done/all) para os badges do filtro.

    O banco é a fonte: todo job (ativo ou terminal) tem uma linha lá e as
    transições de status persistem na hora (via _event), então o `status` no
    banco está sempre atualizado — não precisa somar a memória por cima.
    """
    by_status = store.count_jobs_by_status()
    c = {"all": sum(by_status.values()), "active": 0, "error": 0, "done": 0}
    for group, statuses in _GROUPS.items():
        c[group] = sum(by_status.get(s, 0) for s in statuses)
    return c


def _slim_job(job: dict) -> dict:
    """Job enxuto para os cards da lista de Downloads: sem search/eventos/
    candidatos. Progresso vem só como percentual (a lista não mostra ETA/velo-
    cidade detalhados — isso é o detalhe do job)."""
    return {
        "id": job["id"], "tmdb_id": job.get("tmdb_id"), "language": job["language"],
        "mode": job.get("mode"), "kind": job.get("kind", "both"),
        "download_only": job.get("download_only", False),
        "convert": bool(job.get("convert")),
        "status": job["status"], "detail": job.get("detail", ""),
        "movie": job.get("movie"), "created_at": job["created_at"],
        "destination_label": job.get("destination_label"),
        "video_torrent": job.get("video_torrent"),
        "audio_torrent": job.get("audio_torrent"),
        "output": job.get("output"),
        "progress": {
            "video": _pct(job["progress"].get("video")),
            "audio": _pct(job["progress"].get("audio")),
            "merge": (job["progress"].get("merge") or {}).get("pct")
            if job["progress"].get("merge") else None,
        },
    }


def list_group(group: str = "active") -> list[dict]:
    """Cards enxutos da tela de Downloads, filtrados por grupo NO BACKEND."""
    if group == "all":
        jobs_ = _all_jobs()
    elif group == "active":
        # ativos vivem todos em memória
        jobs_ = [j for j in _jobs.values() if j["status"] in _GROUPS["active"]]
    else:
        statuses = _GROUPS.get(group)
        if not statuses:
            raise ValueError(f"grupo inválido: {group!r}")
        jobs_ = store.load_jobs_by_status(statuses)
    jobs_.sort(key=lambda j: j["created_at"], reverse=True)
    return [_slim_job(j) for j in jobs_]


def progress(job_id: str) -> dict | None:
    """Só status + detail + progresso, para o tick de 1s do detalhe do job."""
    job = _lookup(job_id)
    if not job:
        return None
    return {"id": job["id"], "status": job["status"], "detail": job.get("detail", ""),
            "progress": job["progress"], "output": job.get("output")}


def _event(job: dict, kind: str, message: str, data=None):
    ev = {"ts": datetime.now().isoformat(timespec="seconds"), "kind": kind, "message": message}
    if data is not None:
        ev["data"] = data
    store.add_event(job["id"], ev)
    store.upsert_job(job)  # status/detail quase sempre mudam junto com o evento


def _set(job: dict, status: str, detail: str = ""):
    job["status"] = status
    job["detail"] = detail
    _event(job, "status", detail or status)  # persiste o estado final no banco
    if status == "done":
        # entrou filme novo no destino: a próxima busca refaz o scan da coleção
        catalog.invalidate_library()
    if status in _TERMINAL_STATUSES:
        # virou histórico: tira da memória (quem chamou ainda tem a referência
        # do dict para terminar o que estava fazendo; o banco já está atualizado)
        _jobs.pop(job["id"], None)


def _fail(job: dict, message: str):
    _set(job, "error", message)


# Tipos de job: o que baixar/entregar.
#   both     -> baixa vídeo original + áudio dublado e faz o merge (padrão)
#   original -> baixa só o vídeo original e entrega direto (sem merge)
#   dubbed   -> baixa só a versão dublada e entrega direto (sem merge)
KINDS = ("both", "original", "dubbed")


def _needed_torrents(job: dict) -> tuple[str, ...]:
    """Quais torrents este job baixa: ('video',), ('audio',) ou os dois."""
    kind = job.get("kind", "both")
    if kind == "original":
        return ("video",)
    if kind == "dubbed":
        return ("audio",)
    return ("video", "audio")


async def create(tmdb_id: int, language: str, mode: str = "auto",
                 destination_id: int | None = None,
                 torrent_target_id: int | None = None,
                 kind: str = "both", download_only: bool = False,
                 convert: dict | None = None) -> dict:
    if kind not in KINDS:
        raise ValueError(f"kind inválido: {kind!r}")
    if download_only:
        convert = None  # apenas baixar: nunca há conversão
    if convert is not None:
        convert = transcode.validate(convert).to_dict()
    # readicionar o filme descarta erro anterior da MESMA variante (tmdb+idioma+
    # kind). Concluído/cancelado persistem (não são apagados na readição).
    for old_id in store.error_jobs_for(tmdb_id, language, kind):
        _jobs.pop(old_id, None)
        store.delete_job(old_id)
    # apenas baixar: o produto final fica na pasta dos torrents, então o job
    # não precisa (nem usa) um destino de arquivo final
    dest = None
    if not download_only:
        dest = store.get_destination(destination_id) if destination_id else None
        if dest is None:
            dest = store.default_destination()
        if dest is None:
            raise ValueError("Nenhum destino cadastrado — cadastre uma pasta de destino antes")

    # destino dos torrents e opcional: sem ele, usa pasta padrao do qBittorrent
    # e nao traduz o content_path (comportamento antigo do .env)
    target = store.get_torrent_target(torrent_target_id) if torrent_target_id else None
    if target is None:
        target = store.default_torrent_target()

    job = {
        "id": uuid.uuid4().hex[:10],
        "tmdb_id": tmdb_id,
        "language": language,
        "mode": mode,
        "kind": kind,
        "download_only": download_only,
        "convert": convert,
        "status": "searching",
        "detail": "Buscando informações do filme...",
        "movie": None,
        "video_torrent": None,
        "audio_torrent": None,
        "progress": {"video": None, "audio": None},
        "output": None,
        "destination_id": dest["id"] if dest else None,
        "destination_label": dest["label"] if dest else None,
        "destination_path": dest["path"] if dest else None,
        "torrent_target_id": target["id"] if target else None,
        "torrent_target_label": target["label"] if target else None,
        "torrent_save_path": (target["save_path"] if target else "") or "",
        "torrent_local_path": (target["local_path"] if target else "") or "",
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "search": None,     # candidatos viaveis {audio: [...], video: [...]}
        "fallbacks": None,  # reservas do mesmo corte para o watchdog
        "current": None,    # candidato ativo por kind (com magnet/link p/ reinserir)
    }
    _jobs[job["id"]] = job
    tinfo = f" — torrents: {target['label']}" if target else ""
    kind_label = {"both": "original + dublado (merge)",
                  "original": "só original", "dubbed": "só dublado"}[kind]
    if download_only:
        kind_label = kind_label.replace(" (merge)", "") + ", apenas baixar"
    if convert is not None:
        kind_label += ", conversão customizada"
    dinfo = f" — destino: {dest['label']} ({dest['path']})" if dest else ""
    _event(job, "status", f"Job criado ({kind_label}, modo {mode}){dinfo}{tinfo}")
    if convert is not None:
        _event(job, "info", "Opções avançadas de conversão ativas", convert)
    _spawn(job["id"], _run(job))
    return _public(job)


def _probe_manual_file(path: Path, role: str) -> None:
    """Valida via ffprobe um arquivo de origem da conversão manual.

    O de vídeo precisa de stream de vídeo E de áudio (o alinhamento compara os
    dois áudios); o de áudio precisa só de áudio (pode ser um .mka, ou um vídeo
    dublado inteiro — o merger escolhe o melhor vídeo entre os dois).
    """
    try:
        probe = merger.ffprobe_json(str(path))
    except merger.MergeError:
        raise ValueError(f"'{path.name}' não parece um arquivo de vídeo/áudio válido")
    streams = probe.get("streams", [])
    # capa embutida (attached_pic) não conta como vídeo de verdade
    has_video = any(s.get("codec_type") == "video"
                    and (s.get("disposition") or {}).get("attached_pic") != 1
                    for s in streams)
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    if role == "video" and not has_video:
        raise ValueError(f"'{path.name}' não tem stream de vídeo")
    if not has_audio:
        raise ValueError(f"'{path.name}' não tem stream de áudio "
                         f"(necessário para medir o offset)")


async def create_manual(tmdb_id: int, language: str, video_path: str, audio_path: str,
                        destination_id: int | None = None,
                        convert: dict | None = None) -> dict:
    """Conversão manual: merge de dois arquivos JÁ NO DISCO, sem busca/torrents.

    Mesmo pipeline de conversão dos jobs normais (alinhamento em duas janelas,
    pausa de drift, fila de merge, saída em destino/Filme (Ano)/), só que os
    arquivos vêm de caminhos digitados pelo usuário em vez do qBittorrent.
    """
    if convert is not None:
        convert = transcode.validate(convert).to_dict()
    vf, af = Path(video_path.strip()), Path(audio_path.strip())
    for label, p in (("vídeo", vf), ("áudio", af)):
        if not str(p).strip() or not p.is_file():
            raise ValueError(f"Arquivo de {label} não existe: {p}")
    if vf.resolve() == af.resolve():
        raise ValueError("Os dois caminhos apontam para o mesmo arquivo")
    # ffprobe nos dois em paralelo: rejeita na hora o que não é mídia
    await asyncio.gather(asyncio.to_thread(_probe_manual_file, vf, "video"),
                         asyncio.to_thread(_probe_manual_file, af, "audio"))

    # readicionar o filme descarta erro anterior da mesma variante (como no create)
    for old_id in store.error_jobs_for(tmdb_id, language, "both"):
        _jobs.pop(old_id, None)
        store.delete_job(old_id)
    dest = store.get_destination(destination_id) if destination_id else None
    if dest is None:
        dest = store.default_destination()
    if dest is None:
        raise ValueError("Nenhum destino cadastrado — cadastre uma pasta de destino antes")

    job = {
        "id": uuid.uuid4().hex[:10],
        "tmdb_id": tmdb_id,
        "language": language,
        "mode": "files",  # distingue da busca auto/manual nas listas da UI
        "kind": "both",
        "convert": convert,
        "status": "merging",
        "detail": "Preparando conversão manual...",
        "movie": None,
        "video_torrent": None,
        "audio_torrent": None,
        "progress": {"video": None, "audio": None},
        "output": None,
        "destination_id": dest["id"],
        "destination_label": dest["label"],
        "destination_path": dest["path"],
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "manual_files": {"video": str(vf), "audio": str(af)},
        "search": None,
        "fallbacks": None,
        "current": None,
    }
    _jobs[job["id"]] = job
    _event(job, "status",
           f"Conversão manual criada — vídeo: {vf} | áudio: {af} — "
           f"destino: {dest['label']} ({dest['path']})")
    if convert is not None:
        _event(job, "info", "Opções avançadas de conversão ativas", convert)
    _spawn(job["id"], _run_manual(job, vf, af))
    return _public(job)


async def _run_manual(job: dict, video_file: Path, audio_file: Path):
    """Pipeline da conversão manual: TMDB (só metadados do nome) -> fila -> merge."""
    try:
        movie = await tmdb.details(job["tmdb_id"], job["language"])
        job["movie"] = movie
        _event(job, "info", f"Filme: {movie['original_title']} ({movie['year']})")
        lock = _get_merge_lock()
        if lock.locked():
            _set(job, "merging", "Na fila de conversão — aguardando a conversão anterior terminar...")
        async with lock:
            await _merge(job, video_file, audio_file)
    except asyncio.CancelledError:
        raise
    except merger.VersionMismatch as e:
        _pause_for_drift(job, {"video": video_file, "audio": audio_file}, e)
    except Exception as e:  # noqa: BLE001
        _fail(job, f"{type(e).__name__}: {e}")


# -------------------- acoes da UI --------------------

async def select(job_id: str, audio_id: str | None, video_id: str | None) -> dict | None:
    """Continuacao do modo manual: usuario escolheu o(s) torrent(s)."""
    job = _jobs.get(job_id)
    if not job or job["status"] != "awaiting":
        return None
    search = job.get("search") or {}
    needed = _needed_torrents(job)
    a = v = None
    if "audio" in needed:
        a = next((c for c in search.get("audio", []) if c["id"] == audio_id), None)
        if not a:
            raise ValueError("Candidato de áudio não encontrado (a busca pode ter sido refeita)")
    if "video" in needed:
        v = next((c for c in search.get("video", []) if c["id"] == video_id), None)
        if not v:
            raise ValueError("Candidato de vídeo não encontrado (a busca pode ter sido refeita)")
    # awaiting também acontece na pausa de drift (possível versão diferente);
    # se o usuário preferiu outro torrent em vez de Continuar, a pausa caduca
    job.pop("drift_confirm", None)
    _event(job, "chosen", "Seleção manual do usuário")
    _spawn(job["id"], _download_and_merge(job, a, v))
    return _public(job)


async def switch(job_id: str, kind: str, candidate_id: str | None = None) -> dict | None:
    """Troca manual de torrent durante o download.

    Sem candidate_id: "Tentar próximo" — pega o primeiro candidato reserva.
    Com candidate_id: troca para o candidato escolhido na lista da busca.
    """
    job = _jobs.get(job_id)
    if not job:
        return None
    if job["status"] != "downloading":
        raise ValueError("O job não está baixando — só dá para trocar torrent durante o download")
    if kind not in _needed_torrents(job):
        raise ValueError(f"Este job não baixa {kind}")
    if candidate_id:
        cands = (job.get("search") or {}).get(kind) or []
        nxt = next((c for c in cands if c["id"] == candidate_id), None)
        if not nxt:
            raise ValueError("Candidato não encontrado (a busca pode ter sido refeita)")
    else:
        fb = (job.get("fallbacks") or {}).get(kind) or []
        if not fb:
            raise ValueError(f"Sem candidato reserva de {kind} para tentar")
        nxt = fb[0]
    cur = (job.get("current") or {}).get(kind)
    if cur and cur.get("id") == nxt["id"]:
        raise ValueError("Este já é o torrent atual")
    torrents = await _qbit.info_by_tag(_tag(job, kind))
    current = torrents[0] if torrents else None
    await _replace_torrent(job, kind, current, nxt, "🔁 Troca manual pelo usuário")
    return _public(job)


async def cancel(job_id: str, delete_torrents: bool = False) -> dict | None:
    # ativo: memória; terminal (histórico): banco. Cancelar/limpar só faz sentido
    # no ativo — no terminal só devolvemos o job (para o remove seguir).
    job = _lookup(job_id)
    if not job:
        return None
    is_active = job_id in _jobs
    if is_active:
        task = _tasks.pop(job_id, None)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    if delete_torrents and is_active and not job.get("manual_files"):
        # limpeza no qBittorrent com teto de tempo: se ele estiver fora do ar,
        # a remoção do job NÃO pode ficar travada esperando o timeout de rede
        for kind in ("video", "audio"):
            try:
                await asyncio.wait_for(
                    _qbit.delete_by_tag(_tag(job, kind), delete_files=True),
                    timeout=10)
            except (Exception, asyncio.TimeoutError) as e:  # noqa: BLE001
                reason = "sem resposta (qBittorrent fora do ar?)" if isinstance(
                    e, asyncio.TimeoutError) else str(e)
                _event(job, "qbit",
                       f"⚠️ Não removi o torrent de {kind}: {reason} — "
                       f"apague manualmente no qBittorrent se precisar")
    if job["status"] not in _TERMINAL_STATUSES:
        _set(job, "cancelled",
             "Cancelado pelo usuário" + (" (torrents removidos)" if delete_torrents else ""))
    return job


async def remove(job_id: str, delete_torrents: bool = False) -> bool:
    job = await cancel(job_id, delete_torrents)
    if not job:
        return False
    _jobs.pop(job_id, None)
    store.delete_job(job_id)
    return True


async def retry(job_id: str) -> dict | None:
    old = _lookup(job_id)  # jobs em erro/cancelados vivem só no banco agora
    if not old or old["status"] not in ("error", "cancelled"):
        return None
    if old.get("manual_files"):
        return await create_manual(old["tmdb_id"], old["language"],
                                   old["manual_files"]["video"],
                                   old["manual_files"]["audio"],
                                   old.get("destination_id"), old.get("convert"))
    return await create(old["tmdb_id"], old["language"], old.get("mode", "auto"),
                        old.get("destination_id"), old.get("torrent_target_id"),
                        old.get("kind", "both"), old.get("download_only", False),
                        old.get("convert"))


# -------------------- pipeline --------------------

async def _run(job: dict):
    try:
        await _search(job)
        if job["mode"] == "manual":
            _set(job, "awaiting",
                 "Busca concluída — clique em Escolher para selecionar os torrents")
            return
        a, v = _auto_pick(job)
        await _start_download(job, a, v)
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001 - job nunca deve derrubar o servidor
        _fail(job, f"{type(e).__name__}: {e}")
        return
    await _run_from_download(job)


async def _download_and_merge(job: dict, a: dict | None, v: dict | None):
    try:
        await _start_download(job, a, v)
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001
        _fail(job, f"{type(e).__name__}: {e}")
        return
    await _run_from_download(job)


def _slim(cand: dict, cid: str) -> dict:
    return {"id": cid, "title": cand["title"], "tracker": cand.get("tracker"),
            "seeders": cand["seeders"], "size": cand["size"],
            "edition": cand.get("edition"), "score": cand["score"],
            "magnet": cand.get("magnet"), "link": cand.get("link")}


def _torrent_identity(r: dict) -> str | None:
    """Identidade ESTÁVEL do torrent para dedup, ignorando tokens voláteis.

    - magnet: usa o hash btih (canônico; ignora trackers/dn do magnet).
    - link do Jackett (/dl/...): a URL inteira NÃO serve — o parâmetro `path`
      é um token efêmero, diferente a cada busca do MESMO release. Usamos o
      parâmetro `file` (nome do release), que é estável. Sem `file`, cai para o
      link sem a query string.
    Retorna None quando não há magnet nem link (o rank rejeita depois)."""
    mag = r.get("magnet")
    if mag:
        m = re.search(r"btih:([0-9a-zA-Z]+)", mag)
        return f"hash:{m.group(1).lower()}" if m else f"magnet:{mag}"
    link = r.get("link")
    if link:
        m = re.search(r"[?&]file=([^&]+)", link)
        if m:
            return f"file:{unquote_plus(m.group(1)).lower()}"
        return f"link:{link.split('?', 1)[0]}"  # sem query volátil
    return None


def _dedup_results(results: list[dict]) -> list[dict]:
    """Remove torrents repetidos mantendo a 1ª ocorrência.

    As buscas adicionais (variantes de grafia × regras × indexers) retornam
    muito o MESMO release, então o pool combinado vem cheio de duplicatas. Sem
    isso, o rank processa/mostra N cópias de cada torrent.

    Chaveia por (identidade estável × PROVIDER): o mesmo torrent vindo de
    trackers diferentes é MANTIDO, porque um tracker às vezes nomeia melhor que
    o outro (mais info no título) — deixamos os dois concorrerem no rank. Só
    colapsa duplicatas do mesmo torrent NO MESMO tracker. Sem identidade, passa
    direto (o rank rejeita depois)."""
    seen = set()
    out = []
    for r in results:
        ident = _torrent_identity(r)
        if ident is not None:
            provider = r.get("tracker_id") or r.get("tracker")
            key = (ident, provider)
            if key in seen:
                continue
            seen.add(key)
        out.append(r)
    return out


def _extra_searches(spellings: list[str], year: str, lang: str) -> list[dict]:
    """Buscas extras direcionadas conforme as regras (idioma x variante x indexers).

    Cruza cada GRAFIA do título localizado (o título base + as variantes de
    caractere especial de title_variants: "&"->"e", pontuação removida...) com
    cada REGRA ESPECIAL (tirar o ano, romano->arábico). Ou seja, para cada
    grafia geramos também a versão sem ano e a versão com o numeral em arábico,
    combinando as duas coisas (ex.: "Velozes e Furiosos IX" -> "Velozes e
    Furiosos 9", "Velozes Furiosos 9" sem ano, etc.).

    Cada combinação só entra se produzir uma query DIFERENTE das buscas normais
    (as grafias já buscadas com ano) e se houver indexers configurados para
    aquela regra no idioma. Retorna uma lista de {query, indexer, variant} —
    uma entrada por indexer.
    """
    rules = store.get_extra_search_rules().get(lang) or {}
    if not spellings or not rules:
        return []

    out: list[dict] = []
    # as grafias já são buscadas COM ano nas buscas normais — não repetir
    seen_queries = {f"{s} {year}".strip().lower() for s in spellings}
    for spelling in spellings:
        arabic = selector._roman_to_arabic(spelling)
        has_roman = arabic != spelling
        # cada regra -> a query que ela gera para ESTA grafia (ou None se n/a)
        variant_query = {
            "no_year": spelling if year else None,
            "roman": f"{arabic} {year}".strip() if has_roman else None,
            "roman_no_year": arabic if (has_roman and year) else None,
        }
        for variant, query in variant_query.items():
            if not query:
                continue
            indexers = rules.get(variant) or []
            if not indexers:
                continue
            if query.lower() in seen_queries:
                continue  # não repete uma query que outra combinação já cobriu
            seen_queries.add(query.lower())
            for idx in indexers:
                out.append({"query": query, "indexer": idx, "variant": variant})
    return out


async def _run_extra_search(job: dict, spec: dict) -> list[dict]:
    """Roda uma busca extra; falha de um indexer não derruba o job."""
    try:
        res = await jackett.search(spec["query"], spec["indexer"])
        _event(job, "search",
               f"Busca extra [{spec['variant']} @ {spec['indexer']}] '{spec['query']}' "
               f"→ {len(res)} resultados")
        return res
    except Exception as e:  # noqa: BLE001
        _event(job, "search",
               f"⚠️ Busca extra [{spec['variant']} @ {spec['indexer']}] '{spec['query']}' "
               f"falhou: {type(e).__name__}: {e}")
        return []


async def _search(job: dict):
    """Busca no Jackett e preenche job["search"] com os candidatos viáveis."""
    lang = job["language"]
    label = config.LANGUAGES[lang]["label"]
    movie = await tmdb.details(job["tmdb_id"], lang)
    job["movie"] = movie
    original, localized, year = movie["original_title"], movie["localized_title"], movie["year"]
    _event(job, "info", f"Filme: {original} ({year}) — título em {label}: {localized}")

    needed = _needed_torrents(job)
    want_video = "video" in needed
    want_audio = "audio" in needed

    query_original = f"{original} {year}".strip()
    has_localized = bool(localized and localized.lower() != (original or "").lower())
    query_localized = f"{localized} {year}".strip() if has_localized else None

    # grafias do título localizado: o próprio + variantes de caractere especial
    # (& vs "e", @ vs a, ...). SÓ para o localizado (dublado), onde os trackers
    # BR bagunçam a grafia. O original fica como o TMDB dá (buscar "Fast e
    # Furious" não faz sentido). include_and=False: em português "and" é ruído.
    loc_spellings = ([localized] + selector.title_variants(localized, include_and=False)
                     if has_localized and want_audio else [])
    # as variantes (sem o título base, que já vira query_localized) buscadas com ano
    loc_variants = [f"{v} {year}".strip() for v in loc_spellings[1:]]

    # buscas extras direcionadas (só afetam o áudio dublado): cruzam CADA grafia
    # acima com as regras especiais (tirar ano, romano->arábico) e rodam em
    # paralelo por indexer configurado.
    extra_specs = _extra_searches(loc_spellings, year, lang) if want_audio else []

    _set(job, "searching",
         f"Procurando '{query_original}' no Jackett (pode levar vários minutos)...")
    if extra_specs:
        _event(job, "search",
               f"{len(extra_specs)} busca(s) extra(s) configurada(s) para {label} "
               f"— rodando em paralelo")
    if loc_variants:
        _event(job, "search",
               f"Variantes de grafia do título em {label} — buscando também: "
               f"{', '.join(repr(v) for v in loc_variants)}")

    # dispara TODAS as buscas em paralelo. Guardamos os índices de cada grupo
    # para separar os resultados depois.
    tasks = [jackett.search(query_original)]
    if query_localized:
        tasks.append(jackett.search(query_localized))
    i_loc_var = len(tasks)
    for q in loc_variants:
        tasks.append(jackett.search(q))
    i_extra = len(tasks)
    for spec in extra_specs:
        tasks.append(_run_extra_search(job, spec))
    all_results = await asyncio.gather(*tasks)

    results_original = _dedup_results(all_results[0])
    _event(job, "search", f"Jackett devolveu {len(results_original)} resultados para '{query_original}'")
    idx = 1
    results_localized = []
    if query_localized:
        results_localized = all_results[idx]
        idx += 1
    loc_variant_results = all_results[i_loc_var:i_extra]
    extra_results = all_results[i_extra:]  # já logados dentro de _run_extra_search
    for q, r in zip(loc_variants, loc_variant_results):
        _event(job, "search", f"Variante '{q}' → {len(r)} resultados")

    # ---- audio dublado: titulo traduzido + titulo original com marcador ----
    audio_viable = []
    if want_audio:
        _set(job, "searching", f"Avaliando versão em {label}...")
        audio_ranked = []
        # resultados do título traduzido + variantes de grafia + buscas extras
        # entram como tier 0 (título no idioma dublado tem preferência máxima).
        # dedup: essas buscas repetem MUITO o mesmo release entre si.
        localized_pool = list(results_localized)
        for r in loc_variant_results:
            localized_pool.extend(r)
        for r in extra_results:
            localized_pool.extend(r)
        localized_pool = _dedup_results(localized_pool)
        # dubbed_title: passa o localizado só quando ≠ do original — aí
        # "título dublado + dual" conta como marcador forte (ver marker_strength)
        dubbed_title = localized if has_localized else None
        if localized_pool:
            ranked, trace = selector.rank(localized_pool, "audio", localized, year,
                                          language=lang, dubbed_title=dubbed_title)
            _event(job, "candidates", f"Avaliação para ÁUDIO — título em {label} (+ buscas extras)",
                   {"role": "audio", "query": query_localized or localized, "candidates": trace})
            for c in ranked:
                c["tier"] = 0  # titulo no idioma dublado: preferencia maxima
            audio_ranked.extend(ranked)

        ranked, trace = selector.rank(results_original, "audio", original, year,
                                      language=lang, require_language=True,
                                      dubbed_title=dubbed_title)
        _event(job, "candidates",
               f"Avaliação para ÁUDIO — busca '{query_original}' exigindo marcador de {label}",
               {"role": "audio", "query": query_original, "candidates": trace})
        for c in ranked:
            # titulo original MAS com o titulo traduzido junto (release "Título / Title")
            # ainda conta como dublado confirmado; senao e so fallback
            c["tier"] = 0 if localized and selector.matches_title(c["title"], localized) else 1
        audio_ranked.extend(ranked)

        # dedupe (o mesmo torrent pode aparecer nas duas buscas) e ordena:
        # titulo dublado (tier 0) SEMPRE antes de ingles+marcador (tier 1);
        # score decide dentro de cada tier. Mesma chave (identidade × provider)
        # do _dedup_results: mesmo torrent em trackers diferentes continua concorrendo.
        seen = set()
        for c in sorted(audio_ranked, key=lambda r: (r.get("tier", 1), -r["score"])):
            ident = _torrent_identity(c)
            key = (ident, c.get("tracker_id") or c.get("tracker"))
            if ident is not None and key in seen:
                continue
            seen.add(key)
            audio_viable.append(c)
        n_localized = sum(1 for c in audio_viable if c.get("tier") == 0)
        if n_localized and n_localized < len(audio_viable):
            _event(job, "info",
                   f"Preferência de áudio: {n_localized} candidato(s) com título em {label} "
                   f"na frente de {len(audio_viable) - n_localized} em inglês com marcador")

    # ---- video: titulo original, qualquer corte (o filtro vem depois) ----
    video_viable = []
    if want_video:
        video_viable, trace = selector.rank(results_original, "video", original, year)
        _event(job, "candidates", f"Avaliação para VÍDEO — busca '{query_original}'",
               {"role": "video", "query": query_original, "candidates": trace})

    if want_audio and not audio_viable:
        raise RuntimeError(f"Nenhum torrent encontrado com áudio em {label}")
    if want_video and not video_viable:
        raise RuntimeError(f"Nenhum torrent de vídeo viável para '{original}'")

    job["search"] = {
        "audio": [_slim(c, f"a{i}") for i, c in enumerate(audio_viable[:MAX_SELECTABLE])],
        "video": [_slim(c, f"v{i}") for i, c in enumerate(video_viable[:MAX_SELECTABLE])],
    }
    store.upsert_job(job)


def _auto_pick(job: dict) -> tuple[dict | None, dict | None]:
    """Escolhe o(s) torrent(s) automaticamente conforme o tipo do job.

    - dubbed:   melhor áudio (sem vídeo).
    - original: melhor vídeo (sem áudio).
    - both:     melhor áudio define o corte; melhor vídeo do MESMO corte.
    """
    search = job["search"]
    needed = _needed_torrents(job)
    if needed == ("audio",):
        return search["audio"][0], None
    if needed == ("video",):
        return None, search["video"][0]

    for a in search["audio"]:
        ed_label = a["edition"] or "normal"
        vids = [v for v in search["video"] if v["edition"] == a["edition"]]
        if vids:
            _event(job, "info",
                   f"Corte definido pelo áudio: '{ed_label}' — {len(vids)} vídeos compatíveis")
            return a, vids[0]
        _event(job, "info",
               f"Nenhum vídeo com corte '{ed_label}' para casar com "
               f"'{a['title']}' — tentando o próximo candidato de áudio")
    raise RuntimeError(
        "Nenhum torrent de vídeo com o mesmo corte das versões dubladas encontradas "
        "(as duas versões precisam ser do mesmo corte para os áudios alinharem)")


async def _start_download(job: dict, a: dict | None, v: dict | None):
    if a:
        _event(job, "chosen", f"🔊 Áudio: {a['title']} (score {a['score']}, "
                              f"{a['seeders']} seeds, corte {a['edition'] or 'normal'})")
        job["audio_torrent"] = {"title": a["title"], "seeders": a["seeders"],
                                "size": a["size"], "score": a["score"], "edition": a["edition"]}
    if v:
        _event(job, "chosen", f"🎥 Vídeo: {v['title']} (score {v['score']}, "
                              f"{v['seeders']} seeds, corte {v['edition'] or 'normal'})")
        job["video_torrent"] = {"title": v["title"], "seeders": v["seeders"],
                                "size": v["size"], "score": v["score"], "edition": v["edition"]}

    # reservas do mesmo corte, para o watchdog trocar se o download travar
    search = job.get("search") or {"audio": [], "video": []}
    job["fallbacks"] = {
        "audio": [x for x in search["audio"]
                  if a and x["edition"] == a["edition"] and x["id"] != a["id"]],
        "video": [x for x in search["video"]
                  if v and x["edition"] == v["edition"] and x["id"] != v["id"]],
    }
    # candidato ativo (com magnet/link): usado para reinserir se o torrent
    # sumir do qBittorrent e para a troca manual saber o que está rodando
    job["current"] = {"video": v, "audio": a}

    _set(job, "searching", "Enviando torrents para o qBittorrent...")
    save_path = job.get("torrent_save_path") or config.QBIT_SAVE_PATH or None
    url_video = (v.get("magnet") or v["link"]) if v else None
    url_audio = (a.get("magnet") or a["link"]) if a else None

    if v and a and url_video == url_audio:
        # mesmo torrent serve para os dois (ex.: release dual audio)
        await _qbit.add(url_video, f"{_tag(job, 'video')},{_tag(job, 'audio')}", save_path)
        _event(job, "qbit", "Mesmo torrent serve para vídeo e áudio — adicionado uma única vez")
    else:
        if v:
            await _qbit.add(url_video, _tag(job, "video"), save_path)
            _event(job, "qbit", f"Torrent de vídeo adicionado ao qBittorrent (tag {_tag(job, 'video')})")
        if a:
            await _qbit.add(url_audio, _tag(job, "audio"), save_path)
            _event(job, "qbit", f"Torrent de áudio adicionado ao qBittorrent (tag {_tag(job, 'audio')})")
    if save_path:
        _event(job, "qbit", f"Salvando em: {save_path}")
    _set(job, "downloading", "Baixando torrent..." if len(_needed_torrents(job)) == 1
         else "Baixando torrents...")


def _tag(job: dict, kind: str) -> str:
    return f"dl-{job['id']}-{kind}"


async def _run_from_download(job: dict):
    try:
        paths = await _wait_downloads(job)
        if job.get("download_only"):
            # apenas baixar: o download É o produto final. Nada de conversão,
            # hardlink ou cópia — e os torrents ficam no qBittorrent (seedando),
            # já que os dados baixados são exatamente o que o usuário quer.
            job["output"] = " | ".join(paths[k] for k in ("video", "audio") if k in paths)
            _set(job, "done", f"Concluído — apenas baixado (sem conversão): {job['output']}")
            return
        # localiza os arquivos ANTES de entrar na fila de conversão (com retry:
        # o qBittorrent pode segurar o arquivo recém-concluído por um tempo)
        files = {kind: await _resolve_video_file(job, content, kind)
                 for kind, content in paths.items()}
        # merge (ffmpeg) e entrega single (hardlink/cópia) entram na mesma fila:
        # só 1 por vez, para uma cópia grande não concorrer com uma conversão.
        lock = _get_merge_lock()
        if lock.locked():
            _set(job, "merging", "Na fila de conversão — aguardando a conversão anterior terminar...")
        async with lock:
            if len(_needed_torrents(job)) == 1:
                await _deliver_single(job, files)
            else:
                await _merge(job, files["video"], files["audio"])
    except asyncio.CancelledError:
        raise
    except merger.VersionMismatch as e:
        _pause_for_drift(job, files, e)  # já fora do lock: a fila fica livre
    except Exception as e:  # noqa: BLE001
        _fail(job, f"{type(e).__name__}: {e}")


def _pause_for_drift(job: dict, files: dict, e: "merger.VersionMismatch"):
    """Conversão abortada antes do ffmpeg pesado: as duas janelas de offset
    divergem, então o áudio provavelmente é de outro corte/versão e o resultado
    sairia dessincronizado. Em vez de gastar CPU à toa, o job para em
    'awaiting' (bolinha vermelha de resposta pendente) e o usuário decide:
    Continuar mesmo assim (proceed), escolher outro torrent ou cancelar."""
    job["progress"]["merge"] = None
    job["drift_confirm"] = {
        "video_file": str(files["video"]), "audio_file": str(files["audio"]),
        "tau1_ms": e.tau1_ms, "tau2_ms": e.tau2_ms,
    }
    _set(job, "awaiting",
         f"⚠️ Possível versão/corte diferente: os offsets divergem entre o início "
         f"({e.tau1_ms:+.0f} ms) e o meio ({e.tau2_ms:+.0f} ms) do filme. "
         f"Conversão pausada — clique em Continuar para converter mesmo assim, "
         f"escolha outro torrent ou cancele.")


async def proceed(job_id: str) -> dict | None:
    """'Continuar' após a pausa de drift: converte mesmo com offsets divergentes."""
    job = _jobs.get(job_id)
    if not job or job["status"] != "awaiting" or not job.get("drift_confirm"):
        return None
    info = job.pop("drift_confirm")
    _event(job, "chosen", "Usuário mandou converter mesmo com offsets divergentes")
    _spawn(job["id"], _resume_merge(job, Path(info["video_file"]),
                                    Path(info["audio_file"])))
    return _public(job)


async def _resume_merge(job: dict, video_file: Path, audio_file: Path):
    """Retoma o merge pausado pelo drift, agora com allow_drift=True (a medição
    das janelas se repete, mas isso custa segundos — o caro é o re-encode)."""
    try:
        lock = _get_merge_lock()
        if lock.locked():
            _set(job, "merging", "Na fila de conversão — aguardando a conversão anterior terminar...")
        async with lock:
            await _merge(job, video_file, audio_file, allow_drift=True)
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001
        _fail(job, f"{type(e).__name__}: {e}")


async def _resolve_video_file(job: dict, content_path: str, kind: str) -> Path:
    """Localiza o arquivo de vídeo com retry para erros transitórios de I/O.

    No WSL, stat/listagem em /mnt/* (drvfs/9p) falha com EINVAL enquanto um
    processo Windows (o qBittorrent, logo após concluir) ainda segura o
    arquivo. Espera com backoff em vez de falhar o job.
    """
    delays = (5, 15, 30, 60, 120, 300, 600)  # ~19 min no total
    for i, delay in enumerate(delays):
        try:
            return await asyncio.to_thread(_find_video_file, job, content_path)
        except OSError as e:
            if i == 0:
                _event(job, "info",
                       f"⚠️ Arquivo de {kind} ainda inacessível ({e}) — o qBittorrent "
                       f"pode estar verificando/movendo o download; aguardando...")
            job["detail"] = (f"Aguardando o arquivo de {kind} ficar acessível "
                             f"(tentativa {i + 1}/{len(delays) + 1})...")
            await asyncio.sleep(delay)
    # última tentativa: se ainda falhar, o erro real sobe e o job falha
    return await asyncio.to_thread(_find_video_file, job, content_path)


def _stall_limit_minutes(state: str) -> int:
    """Timeout de stall conforme o estado do torrent.

    metaDL (ainda buscando metadados do magnet) merece mais paciência: 30 min.
    stalledDL (sem seed disponível AGORA) pode voltar sozinho: 2 h.
    Os valores nunca ficam abaixo do STALL_TIMEOUT_MINUTES configurado.
    """
    s = state.lower()
    if "metadl" in s:       # metaDL / forcedMetaDL
        return max(config.STALL_TIMEOUT_MINUTES, 30)
    if "stalleddl" in s:    # stalledDL
        return max(config.STALL_TIMEOUT_MINUTES, 120)
    return config.STALL_TIMEOUT_MINUTES


async def _wait_downloads(job: dict) -> dict:
    """Espera os torrents necessários terminarem; watchdog troca torrent travado.

    Perder a conexão com o qBittorrent NUNCA falha o job: avisa uma vez,
    mantém o estado "baixando" e fica tentando reconectar. Torrent que sumiu
    do qBittorrent é reinserido automaticamente — só para de reinserir se o
    job for cancelado/excluído.
    """
    needed = _needed_torrents(job)
    paths = {}
    stall = {k: {"pct": -1.0, "since": time.monotonic(), "warned": False, "hash": None}
             for k in needed}
    # magnet adicionado pode levar alguns segundos ate aparecer em /info
    # (qBittorrent ainda buscando metadados). so tratamos como "removido" se
    # sumir por um bom tempo, nao na primeira consulta.
    missing = {k: {"since": None} for k in needed}
    METADATA_GRACE = max(config.STALL_TIMEOUT_MINUTES, 5) * 60
    conn_lost = False
    # o progresso muda a cada consulta, mas persistir a cada ciclo (1.5s) é
    # desperdício: gravamos o "mero progresso" no máximo 1x por PROGRESS_PERSIST_
    # SECONDS. Eventos reais (concluído, troca, warning...) já persistem sozinhos
    # via _event, então nunca dependem deste relógio.
    last_persist = 0.0
    while len(paths) < len(needed):
        try:
            for kind in needed:
                if kind in paths:
                    continue
                torrents = await _qbit.info_by_tag(_tag(job, kind))
                if conn_lost:
                    conn_lost = False
                    _event(job, "qbit", "✅ Conexão com o qBittorrent restabelecida")
                    job["detail"] = ("Baixando torrent..." if len(needed) == 1
                                     else "Baixando torrents...")
                    # o tempo desconectado não conta como stall nem como sumiço
                    now = time.monotonic()
                    for st_ in stall.values():
                        st_["since"] = now
                    for m_ in missing.values():
                        m_["since"] = None
                if not torrents:
                    miss = missing[kind]
                    if miss["since"] is None:
                        miss["since"] = time.monotonic()
                        _event(job, "qbit",
                               f"Torrent de {kind} ainda não aparece no qBittorrent "
                               f"(buscando metadados do magnet)...")
                    elif time.monotonic() - miss["since"] > METADATA_GRACE:
                        # sumiu (removido à mão?) ou magnet nunca materializou:
                        # reinsere e recomeça a espera, sem falhar o job
                        await _readd_torrent(job, kind)
                        miss["since"] = time.monotonic()
                    continue
                missing[kind]["since"] = None
                t = torrents[0]
                pct = t.get("progress", 0)
                job["progress"][kind] = {
                    "pct": round(pct * 100, 1),
                    "speed": t.get("dlspeed", 0),
                    "eta": t.get("eta"),
                    "state": t.get("state"),
                    "seeds": t.get("num_seeds", 0),
                    "name": t.get("name"),
                }
                if pct >= 1:
                    paths[kind] = t["content_path"]
                    _event(job, "qbit", f"Download de {kind} concluído: {t['content_path']}")
                    continue

                st = stall[kind]
                state = t.get("state") or ""
                if t.get("hash") != st["hash"]:
                    # torrent trocado (watchdog ou troca manual): zera o relógio
                    st.update(hash=t.get("hash"), pct=-1.0,
                              since=time.monotonic(), warned=False)
                limit_min = _stall_limit_minutes(state)
                if state in ("stoppedDL", "pausedDL"):
                    # usuário parou o torrent manualmente: não conta o tempo de
                    # stall (o relógio recomeça do zero quando ele retomar)
                    st.update(since=time.monotonic(), warned=False)
                elif pct > st["pct"] + 1e-4:
                    st.update(pct=pct, since=time.monotonic(), warned=False)
                elif (config.STALL_TIMEOUT_MINUTES > 0
                      and time.monotonic() - st["since"] > limit_min * 60):
                    if await _switch_torrent(job, kind, t, limit_min):
                        st.update(pct=-1.0, since=time.monotonic(), warned=False)
                    elif not st["warned"]:
                        _event(job, "qbit",
                               f"⚠️ Download de {kind} sem progresso há "
                               f"{limit_min} min e sem candidato reserva — "
                               f"continuando a esperar (cancele o job se quiser desistir)")
                        st["warned"] = True
            # persiste o progresso no banco só de tempos em tempos (eventos
            # reais já persistiram na hora, via _event)
            if time.monotonic() - last_persist >= config.PROGRESS_PERSIST_SECONDS:
                store.upsert_job(job)
                last_persist = time.monotonic()
        except _CONN_ERRORS as e:
            # qBittorrent fora do ar / rede caiu: avisa uma vez e segue tentando
            if not conn_lost:
                conn_lost = True
                _event(job, "qbit",
                       f"⚠️ Perdi a conexão com o qBittorrent ({type(e).__name__}: {e}) — "
                       f"mantendo o download e tentando reconectar...")
            job["detail"] = "Sem conexão com o qBittorrent — tentando reconectar..."
        if len(paths) < len(needed):
            await asyncio.sleep(config.POLL_INTERVAL_SECONDS)
    return paths


async def _readd_torrent(job: dict, kind: str):
    """Reinsere o torrent atual de `kind` que sumiu do qBittorrent."""
    cand = (job.get("current") or {}).get(kind)
    if not cand:
        # jobs antigos (sem "current"): tenta achar o candidato pelo título escolhido
        title = (job.get(f"{kind}_torrent") or {}).get("title")
        cands = (job.get("search") or {}).get(kind) or []
        cand = next((c for c in cands if c["title"] == title), None)
    if not cand or not (cand.get("magnet") or cand.get("link")):
        _event(job, "qbit",
               f"⚠️ Torrent de {kind} sumiu do qBittorrent e não tenho o link para "
               f"reinserir — continuando a esperar")
        return
    save_path = job.get("torrent_save_path") or config.QBIT_SAVE_PATH or None
    await _qbit.add(cand.get("magnet") or cand["link"], _tag(job, kind), save_path)
    _event(job, "qbit",
           f"🔁 Torrent de {kind} não está mais no qBittorrent — reinserido: {cand['title']}")


async def _switch_torrent(job: dict, kind: str, current: dict, limit_min: int) -> bool:
    """Watchdog: troca um download travado pelo próximo candidato do mesmo corte."""
    fallbacks = (job.get("fallbacks") or {}).get(kind) or []
    if not fallbacks:
        return False
    nxt = fallbacks[0]
    await _replace_torrent(job, kind, current, nxt,
                           f"⏳ Download de {kind} travado há {limit_min} min — trocado por")
    return True


async def _replace_torrent(job: dict, kind: str, current: dict | None, nxt: dict, reason: str):
    """Substitui o torrent ativo de `kind` por `nxt` (watchdog ou troca manual).

    `current` é o torrent como reportado pelo qBittorrent (pode ser None se ele
    nem chegou a aparecer). O candidato substituído volta para o FIM da lista
    de reservas — dá para tentar de novo mais tarde.
    """
    if not job.get("fallbacks"):
        job["fallbacks"] = {}
    fb = job["fallbacks"].get(kind) or []
    job["fallbacks"][kind] = [c for c in fb if c["id"] != nxt["id"]]
    cur_cand = (job.get("current") or {}).get(kind)
    if cur_cand and cur_cand["id"] != nxt["id"]:
        job["fallbacks"][kind].append(cur_cand)

    tag = _tag(job, kind)
    if current:
        # se o mesmo torrent serve para os dois papéis (dual audio), só tira a tag
        other = "audio" if kind == "video" else "video"
        shared = False
        if other in _needed_torrents(job):
            other_torrents = await _qbit.info_by_tag(_tag(job, other))
            shared = bool(other_torrents) and other_torrents[0].get("hash") == current.get("hash")
        await _qbit.remove_tag(current["hash"], tag)
        if not shared:
            await _qbit.delete(current["hash"], delete_files=True)
    save_path = job.get("torrent_save_path") or config.QBIT_SAVE_PATH or None
    await _qbit.add(nxt.get("magnet") or nxt["link"], tag, save_path)

    job[f"{kind}_torrent"] = {"title": nxt["title"], "seeders": nxt["seeders"],
                              "size": nxt["size"], "score": nxt["score"],
                              "edition": nxt["edition"]}
    if not job.get("current"):
        job["current"] = {}
    job["current"][kind] = nxt
    _event(job, "qbit", f"{reason}: {nxt['title']} ({nxt['seeders']} seeds)")


def _map_qbit_path(job: dict, path: str) -> Path:
    """Traduz o caminho reportado pelo qBittorrent para o caminho local.

    Prioridade: o par save_path->local_path do destino de torrents do job;
    depois o QBIT_PATH_MAP global do .env (fallback/compatibilidade).
    """
    save = job.get("torrent_save_path") or ""
    local = job.get("torrent_local_path") or ""
    if save and local:
        mapped = config.map_path(path, [(save, local)])
        if mapped != path:
            return Path(mapped)
    return Path(config.map_path(path, config.QBIT_PATH_MAP))


def _find_video_file(job: dict, content_path: str) -> Path:
    p = _map_qbit_path(job, content_path)
    if not p.exists():
        raise RuntimeError(
            f"Caminho '{p}' (qBittorrent reportou '{content_path}') não existe nesta máquina. "
            f"Configure o caminho local do destino de torrents em Configurações "
            f"(ou monte a pasta de downloads nesta máquina).")
    if p.is_file():
        return p
    files = [f for f in p.rglob("*")
             if f.suffix.lower() in VIDEO_EXTENSIONS and "sample" not in f.name.lower()]
    if not files:
        raise RuntimeError(f"Nenhum arquivo de vídeo encontrado em {p}")
    return max(files, key=lambda f: f.stat().st_size)


async def _deliver_single(job: dict, files: dict):
    """Job de um torrent só: entrega o arquivo direto no destino, sem merge.

    Com opções avançadas ativas, em vez do hardlink o arquivo único passa pela
    conversão (que por si só cai em hardlink se o plano inteiro der em cópia).
    """
    kind = "video" if "video" in files else "audio"
    src_file = files[kind]
    _event(job, "info", f"Arquivo baixado: {src_file}")

    movie = job["movie"]
    safe_title = re.sub(r'[<>:"/\\|?*]', "", f"{movie['original_title']} ({movie['year']})")
    tag = "orig" if job["kind"] == "original" else job["language"]
    dest_dir = Path(job.get("destination_path") or config.OUTPUT_DIR)
    label = "original" if job["kind"] == "original" else f"dublado ({job['language']})"

    if job.get("convert"):
        opts = transcode.validate(job["convert"])
        output = dest_dir / safe_title / f"{safe_title} [{tag}].mkv"
        _set(job, "merging", f"Convertendo arquivo {label} ({src_file.name})...")

        def log(msg):
            job["detail"] = str(msg)
            _event(job, "merge", str(msg))

        last_persist = [0.0]

        def on_progress(info: dict):
            job["progress"]["merge"] = info
            now = time.monotonic()
            if now - last_persist[0] > 15:
                last_persist[0] = now
                store.upsert_job(job)

        result = await asyncio.to_thread(
            transcode.convert_single, str(src_file), str(output), opts,
            job["language"], (movie or {}).get("original_language"),
            log=log, on_progress=on_progress)
        job["progress"]["merge"] = None
        job["output"] = result.output
        done_label = "entregue (sem conversão necessária)" if result.linked else "convertido"
        _set(job, "done", f"Concluído — {label} {done_label} em: {result.output}")
        await _cleanup_torrents(job)
        return

    output = dest_dir / safe_title / f"{safe_title} [{tag}]{src_file.suffix}"
    _set(job, "merging", f"Entregando arquivo {label} no destino...")

    notes: list[str] = []
    # hardlink (fallback cópia) roda em thread para não travar a API em cópias grandes
    await asyncio.to_thread(merger._link_or_copy, src_file, output, notes)
    for n in notes:
        _event(job, "info", n)

    job["output"] = str(output)
    _set(job, "done", f"Concluído — {label} entregue em: {output}")
    await _cleanup_torrents(job)


async def _merge(job: dict, video_file: Path, audio_file: Path,
                 allow_drift: bool = False):
    _event(job, "merge", f"Arquivo de vídeo: {video_file}")
    _event(job, "merge", f"Arquivo de áudio: {audio_file}")

    movie = job["movie"]
    safe_title = re.sub(r'[<>:"/\\|?*]', "", f"{movie['original_title']} ({movie['year']})")
    # subpasta por filme dentro do destino escolhido (bom para Jellyfin/Plex)
    dest_dir = Path(job.get("destination_path") or config.OUTPUT_DIR)
    output = dest_dir / safe_title / f"{safe_title} [{job['language']}+orig].mkv"

    _set(job, "merging", f"Fazendo merge ({video_file.name} + {audio_file.name})...")

    def log(msg):
        job["detail"] = str(msg)
        _event(job, "merge", str(msg))

    # progresso do ffmpeg: atualiza em memória (a UI le via polling); persiste
    # no banco só de vez em quando para não martelar o SQLite a cada tick
    last_persist = [0.0]

    def on_progress(info: dict):
        job["progress"]["merge"] = info
        now = time.monotonic()
        if now - last_persist[0] > 15:
            last_persist[0] = now
            store.upsert_job(job)

    # opções avançadas (se o job tiver): re-valida contra o servidor atual
    convert_opts = transcode.validate(job["convert"]) if job.get("convert") else None

    # merger.merge é bloqueante (ffmpeg/ffprobe); roda em thread para não travar a API
    result = await asyncio.to_thread(
        merger.merge, str(video_file), str(audio_file), str(output),
        job["language"], log=log, on_progress=on_progress,
        allow_drift=allow_drift, convert=convert_opts,
        original_lang=(job.get("movie") or {}).get("original_language"))
    job["progress"]["merge"] = None  # terminou (com sucesso): some a barra

    job["output"] = result.output
    if result.linked:
        _set(job, "done", f"Áudio no idioma alvo já existia no melhor vídeo — hardlink criado: {result.output}")
    else:
        _set(job, "done", f"Concluído (offset {result.offset_ms:+.2f} ms): {result.output}")

    await _cleanup_torrents(job)


async def _cleanup_torrents(job: dict):
    if job.get("manual_files"):
        return  # conversão manual: não há torrents para limpar
    if config.QBIT_CLEANUP == "keep":
        return
    # hardlink/cópia são independentes do arquivo do qBittorrent (nada de symlink),
    # então remove_data pode apagar os dados com segurança mesmo quando só linkou.
    delete_files = config.QBIT_CLEANUP == "remove_data"
    for kind in ("video", "audio"):
        try:
            await _qbit.delete_by_tag(_tag(job, kind), delete_files)
            _event(job, "qbit", f"Torrent de {kind} removido do qBittorrent"
                                + (" (com os dados)" if delete_files else ""))
        except Exception as e:  # noqa: BLE001
            _event(job, "qbit", f"Falha ao remover torrent de {kind}: {e}")
