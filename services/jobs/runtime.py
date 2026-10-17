"""Estado compartilhado e infraestrutura dos jobs.

Registro em memória dos jobs ATIVOS (`_jobs`), tarefas, processos de ffmpeg,
cliente do qBittorrent, fila de conversão, eventos/estado e os helpers que o
resto do pacote usa (`_event`, `_set`, `_fail`, `_spawn`, `_tag`...).

Camada de baixo: não importa nenhum outro módulo do pacote.
"""

import asyncio
import atexit
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

import httpx

import config
from services import candidates, catalog, store
from services.qbittorrent import QbitClient, QbitError


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


# -------------------- modo do job --------------------
# Um job pode vir do qBittorrent (auto/manual), de dois arquivos locais
# (manual_files) ou da recompressão de um filme da coleção (recompress). Em vez
# de espalhar `job.get("manual_files")` / `job.get("recompress")` por cada ação
# que dispara em torrents, o tipo é decidido AQUI e consultado por todos.

def _is_recompress(job: dict) -> bool:
    return job.get("mode") == "recompress" or bool(job.get("recompress"))


def _is_local_files(job: dict) -> bool:
    """Conversão manual de dois arquivos já no disco (sem torrents)."""
    return bool(job.get("manual_files"))


def _has_torrents(job: dict) -> bool:
    """O job baixa/limpa torrents no qBittorrent? (só os modos de download)."""
    return not _is_recompress(job) and not _is_local_files(job)
_tasks: dict[str, asyncio.Task] = {}
# processo ffmpeg ativo por job: cancelar a task async NÃO interrompe o
# subprocess (roda numa thread via to_thread), então guardamos o Popen aqui
# para matá-lo no cancel(). Some quando o merge termina.
_ffmpeg_procs: dict[str, "subprocess.Popen"] = {}
# ids sendo cancelados agora: matar o ffmpeg faz o merge levantar MergeError, e
# esse "erro" NÃO deve virar status=error — é o cancelamento pedido. O handler
# de exceção do pipeline checa aqui para não sobrescrever o estado.
_cancelling: set[str] = set()
_qbit = QbitClient()

# quando a UI pediu progresso pela última vez (time.monotonic()); None = nunca.
# O watchdog só corre rápido enquanto a UI acompanha; sem ninguém lendo, o poll
# rápido é só carga no qBittorrent. None (e não 0.0) porque monotonic() pode
# começar perto de zero, e "nunca pediram" viraria "acabaram de pedir".
_last_progress_demand: float | None = None


def touch_progress_demand():
    """Marca que alguém está acompanhando o progresso agora."""
    global _last_progress_demand
    _last_progress_demand = time.monotonic()


def progress_demanded() -> bool:
    """Alguém pediu progresso na janela recente? (define o ritmo do watchdog)"""
    if _last_progress_demand is None:
        return False
    return (time.monotonic() - _last_progress_demand
            < config.POLL_ACTIVE_WINDOW_SECONDS)


def poll_interval() -> float:
    """Intervalo do watchdog: rápido só com a UI acompanhando."""
    return (config.POLL_INTERVAL_SECONDS if progress_demanded()
            else config.POLL_IDLE_INTERVAL_SECONDS)


# fila de conversão: só 1 merge/entrega roda por vez (ffmpeg é pesado de CPU/IO).
# lazy porque o event loop pode não existir no import.
_merge_lock: asyncio.Lock | None = None


def _get_merge_lock() -> asyncio.Lock:
    global _merge_lock
    if _merge_lock is None:
        _merge_lock = asyncio.Lock()
    return _merge_lock


def _register_proc(job_id: str):
    """on_start para o merger: guarda o Popen do ffmpeg deste job, para que o
    cancel() possa matá-lo. Retorna a função a passar como on_start."""
    def _on_start(proc):
        _ffmpeg_procs[job_id] = proc
    return _on_start


# Etapas de uma conversão, como a UI as nomeia (uma fonte só para o
# dropdown, a lista e o detalhe). As intermediárias saem em âmbar; só
# `convert` é "conversão" de verdade — juntar/converter o arquivo final.
PHASE_ALIGN = "align"      # fingerprint dos dois arquivos: buscando alinhamento
PHASE_EDL = "edl"          # remontando a faixa dublada (arquivo intermediário)
PHASE_CONVERT = "convert"  # merge/render/recompressão de fato


def _ffmpeg_hooks(job: dict, phase: str = PHASE_CONVERT):
    """(log, on_progress) para as conversões de ffmpeg. log escreve no detalhe
    + evento 'merge'; on_progress guarda o progresso em memória (a UI lê via
    polling). Usado por merge, entrega single e recompressão."""
    def log(msg):
        job["detail"] = str(msg)
        _event(job, "merge", str(msg))

    def on_progress(info: dict):
        # só em memória: a barra do merge não vai para o banco (store,
        # _enc_progress) — depois de um restart o merge recomeça do zero, e
        # regravar o job a cada 15s só para ela não guardava nada útil. A
        # ETAPA (phase/step/label) vai junto da próxima gravação do job, para
        # o detalhe de um job que falhou no meio dizer onde ele parou
        job["progress"]["merge"] = {**info, "phase": info.get("phase", phase)}

    return log, on_progress


def _kill_ffmpeg(job_id: str):
    """Mata o ffmpeg em andamento deste job (se houver). Chamado ao cancelar:
    o subprocess roda numa thread, então cancelar a task não o interrompe."""
    proc = _ffmpeg_procs.pop(job_id, None)
    if proc and proc.poll() is None:
        try:
            proc.kill()
        except OSError:
            pass


def _delete_output(job: dict):
    """Apaga o arquivo final (parcial ou pronto) e a subpasta do filme se ficar
    vazia. Usado ao cancelar durante a conversão: o .mkv em construção fica
    corrompido/incompleto e não deve sobrar no destino.

    SÓ apaga o que ESTE job escreveu: o caminho de saída é registrado no
    começo do merge, antes de qualquer escrita — se o arquivo ali é mais
    antigo que o início do merge, ele veio de OUTRO job (duplicado do mesmo
    filme) e cancelar este não pode destruí-lo. Caso real de campo: cancelar
    um job duplicado apagou o filme pronto do job anterior (159 GiB)."""
    out = job.get("output")
    if not out:
        return
    p = Path(out)
    try:
        if p.is_file():
            started = job.get("merge_started_at")
            if started:
                from datetime import datetime
                try:
                    t0 = datetime.fromisoformat(started).timestamp()
                    if p.stat().st_mtime < t0 - 1.0:
                        _event(job, "info",
                               f"Arquivo final preservado (é de outro job, "
                               f"anterior a este merge): {p}")
                        return
                except (ValueError, OSError):
                    pass
            p.unlink()
            _event(job, "info", f"Arquivo final removido: {p}")
        # remove a subpasta do filme se esvaziou (só a criamos para este filme)
        parent = p.parent
        if parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
    except OSError as e:
        _event(job, "info", f"Não removi o arquivo final ({e}) — apague manualmente se precisar")


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


# seções do job que não vão no detalhe como estão: os eventos têm rota
# própria e os candidatos chegam à UI pelas visões (lista de escolha no
# get_job, payload dos gates de série, escolhido de cada papel abaixo). A
# marca de dict carregado sem as EDLs é da persistência, não do job.
_NOT_PUBLIC = ("events", "search_queries", "search_candidates",
               store.EDLS_UNLOADED)


def _public(job: dict) -> dict:
    out = {k: v for k, v in job.items() if k not in _NOT_PUBLIC}
    if job.get("media_type") == "tv":
        if job.get("awaiting"):
            # gates de série deriváveis do estado: payload calculado aqui, não
            # gravado (ver pipeline.DERIVED_GATES)
            from services.series import pipeline
            out["awaiting"] = pipeline.awaiting_view(job)
        # relatório final: derivado do estado dos episódios, nunca gravado
        from services.series import merge_runner
        out["report"] = merge_runner.report_view(job)
    else:
        # filme: o escolhido de cada papel (video_torrent/audio_torrent) e o
        # candidato ativo são atributos do candidato, não cópias no job
        out.update(candidates.movie_fields(job))
        gate = job.get("awaiting")
        if gate and gate.get("reason") == "alignment_review":
            # revisão de alinhamento do filme: a EDL já está em
            # job["advanced"]; o payload é calculado aqui, não gravado
            from services.jobs import advanced
            out["awaiting"] = {**gate, "payload": advanced.review_payload(job)}
    return out


def _lookup(job_id: str) -> dict | None:
    """Job ativo (memória) ou terminal (banco). Para leitura/ações que também
    valem em histórico (ver detalhe, retry, remover). O dict do banco é uma
    cópia — mutar não afeta memória (correto: terminais são imutáveis)."""
    return _jobs.get(job_id) or store.get_job(job_id)


# -------------------- persistência do documento do job --------------------
# O código muta o dict do job e chama _event — e conta, sem dizer, que o evento
# leva a mutação para o banco. Regravar o documento inteiro a CADA evento era
# o custo: um job de série tem centenas de KB e o merge emite milhares de
# linhas de log (cada uma reescrevia o job). Agora o evento só marca o job como
# sujo; o documento grava no máximo uma vez por config.EVENT_PERSIST_SECONDS:
#   - primeiro evento depois de um silêncio (ou o primeiro do job neste
#     processo, que é o que cria a linha): grava na hora;
#   - eventos dentro da janela: job fica em _dirty e o flush periódico
#     (persist_loop, no lifespan do servidor) grava quando a janela fecha;
#   - _set (mudança de status) grava sempre na hora;
#   - desligamento (lifespan e atexit): flush de tudo que estiver sujo.
# Chamado também de threads (log do ffmpeg roda via to_thread), por isso o lock.
_persist_lock = threading.Lock()
_last_persist: dict[str, float] = {}  # job_id -> monotonic() da última gravação
_dirty: dict[str, dict] = {}          # job_id -> dict com mutação ainda não gravada
# jobs removidos neste processo: nada mais grava (nem evento) para eles — um
# flush atrasado ou um evento tardio da task que morria não pode ressuscitar a
# linha que o usuário apagou.
_removed: set[str] = set()


def _persist(job: dict):
    """Grava o documento do job AGORA e o tira da lista de sujos."""
    jid = job["id"]
    with _persist_lock:
        if jid in _removed:
            return
        # limpa ANTES de gravar: mutação feita durante a gravação (outra
        # thread) volta a marcar sujo e sai no próximo flush
        _dirty.pop(jid, None)
        _last_persist[jid] = time.monotonic()
    store.upsert_job(job)


def _touch(job: dict):
    """Mutação a persistir sem urgência: grava já se a janela passou, senão
    marca sujo para o flush."""
    jid = job["id"]
    with _persist_lock:
        if jid in _removed:
            return
        last = _last_persist.get(jid)
        if (last is not None
                and time.monotonic() - last < config.EVENT_PERSIST_SECONDS):
            _dirty[jid] = job
            return
    _persist(job)


def flush_dirty(force: bool = False) -> int:
    """Grava os jobs sujos cuja janela fechou (todos, com force). Devolve
    quantos gravou. Só ATUALIZA (create=False): a linha já foi criada pela
    primeira gravação, e um job apagado por fora (store.delete_job direto)
    não volta."""
    now = time.monotonic()
    with _persist_lock:
        due = [j for jid, j in _dirty.items()
               if force or now - _last_persist.get(jid, 0.0)
               >= config.EVENT_PERSIST_SECONDS]
        for j in due:
            _dirty.pop(j["id"], None)
            _last_persist[j["id"]] = now
    written = 0
    for j in due:
        try:
            store.upsert_job(j, create=False)
            written += 1
        except RuntimeError:
            # job sujo é job em movimento: se uma thread (merge via to_thread)
            # mexeu num dict no meio do json.dumps, tenta de novo no próximo
            # tick em vez de perder a mutação
            with _persist_lock:
                if j["id"] not in _removed:
                    _dirty.setdefault(j["id"], j)
    return written


def _forget(job_id: str):
    """O job foi (ou vai ser) apagado do banco: descarta o que estiver sujo e
    bloqueia gravações futuras dele."""
    with _persist_lock:
        _removed.add(job_id)
        _dirty.pop(job_id, None)
        _last_persist.pop(job_id, None)


def _delete_job(job_id: str):
    """Remove o job da memória e do banco sem deixar flush pendente."""
    _forget(job_id)
    _jobs.pop(job_id, None)
    store.delete_job(job_id)


async def persist_loop():
    """Flush periódico dos jobs sujos (task do lifespan do servidor)."""
    tick = min(1.0, config.EVENT_PERSIST_SECONDS)
    while True:
        await asyncio.sleep(tick)
        try:
            flush_dirty()
        except Exception as e:  # noqa: BLE001 — o loop não pode morrer
            print(f"[jobs] flush dos jobs sujos falhou: {e}")


def _flush_at_exit():
    # scripts que importam o pacote sem o servidor (sem persist_loop) também
    # saem com o estado final gravado
    try:
        flush_dirty(force=True)
    except Exception:  # noqa: BLE001 — banco já fechado/indisponível na saída
        pass


atexit.register(_flush_at_exit)


def _add_event(job: dict, kind: str, message: str, data=None) -> bool:
    """Grava só a linha do evento (sem o documento do job)."""
    if job["id"] in _removed:
        return False  # job apagado: evento tardio viraria linha órfã
    ev = {"ts": datetime.now().isoformat(timespec="seconds"), "kind": kind, "message": message}
    if data is not None:
        ev["data"] = data
    store.add_event(job["id"], ev)
    return True


def _event(job: dict, kind: str, message: str, data=None):
    if _add_event(job, kind, message, data):
        # o documento quase sempre mudou junto com o evento (detail, estado de
        # episódio, hash de torrent...): marca para gravar, com janela
        _touch(job)


def _set(job: dict, status: str, detail: str = ""):
    job["status"] = status
    job["detail"] = detail
    _add_event(job, "status", detail or status)
    # transição de status é durável na hora (não espera a janela do _event)
    _persist(job)
    if status == "done":
        # entrou filme novo no destino: a próxima busca refaz o scan da coleção
        catalog.invalidate_library()
    if status in _TERMINAL_STATUSES:
        # virou histórico: tira da memória (quem chamou ainda tem a referência
        # do dict para terminar o que estava fazendo; o banco já está atualizado)
        _jobs.pop(job["id"], None)


def _fail(job: dict, message: str):
    # o job está sendo cancelado: o erro (ffmpeg morto pelo kill) é esperado —
    # deixa o cancel() definir o estado final, não sobrescreve com "error"
    if job["id"] in _cancelling:
        return
    _set(job, "error", message)


# Tipos de job: o que baixar/entregar.
#   both     -> baixa vídeo original + áudio dublado e faz o merge (padrão)
#   original -> baixa só o vídeo original e entrega direto (sem merge)
#   dubbed   -> baixa só a versão dublada e entrega direto (sem merge)
KINDS = ("both", "original", "dubbed")


def _needed_torrents(job: dict) -> tuple[str, ...]:
    """Quais torrents este job baixa: ('video',), ('audio',) ou os dois.

    Recompressão não baixa torrents (kind=None) — não é chamada no pipeline de
    download, mas o None não pode virar ('video','audio') se algum caminho a
    alcançar por engano."""
    if _is_recompress(job):
        return ()
    kind = job.get("kind") or "both"
    if kind == "original":
        return ("video",)
    if kind == "dubbed":
        return ("audio",)
    return ("video", "audio")


def _tag(job: dict, kind: str) -> str:
    return f"dl-{job['id']}-{kind}"


def _qbit_path_candidates(job: dict, path: str) -> list[Path]:
    """Caminhos locais POSSÍVEIS para um caminho reportado pelo qBittorrent,
    em ordem de prioridade: o par save_path->local_path do destino do job,
    os pares de TODOS os destinos cadastrados e o QBIT_PATH_MAP do .env.

    Todos os destinos, e não só o do job, porque o usuário troca a pasta de
    salvamento no PRÓPRIO qBittorrent quando falta espaço — e aí o caminho
    reportado passa a ter o prefixo de outro destino. O caminho cru entra
    por último (qBittorrent na mesma máquina, sem tradução)."""
    pairs: list[tuple[str, str]] = []
    save = job.get("torrent_save_path") or ""
    local = job.get("torrent_local_path") or ""
    if save and local:
        pairs.append((save, local))
    try:
        targets = store.list_torrent_targets()
    except Exception:  # noqa: BLE001 — sem banco (testes, CLI): só o job e o .env
        targets = []
    pairs += sorted(((t["save_path"], t["local_path"]) for t in targets
                     if t.get("save_path") and t.get("local_path")),
                    key=lambda m: len(m[0]), reverse=True)
    pairs += list(config.QBIT_PATH_MAP)
    out: list[Path] = []
    seen: set[str] = set()
    for src, dst in pairs:
        mapped = config.map_path(path, [(src, dst)])
        if mapped != path and mapped not in seen:
            seen.add(mapped)
            out.append(Path(mapped))
    if path not in seen:
        out.append(Path(path))
    return out


def _map_qbit_path(job: dict, path: str) -> Path:
    """Traduz o caminho reportado pelo qBittorrent para o caminho local: o
    primeiro candidato (ver _qbit_path_candidates) que EXISTE nesta máquina.
    Sem nenhum existente, devolve o de maior prioridade — quem chama decide
    o que fazer com um caminho inexistente (e lista os tentados no erro)."""
    cands = _qbit_path_candidates(job, path)
    for c in cands:
        if c.exists():
            return c
    return cands[0]


def _free_name(path: Path) -> Path:
    """path, ou 'nome (2).ext', 'nome (3).ext'... se já existir."""
    if not path.exists():
        return path
    for i in range(2, 100):
        cand = path.with_name(f"{path.stem} ({i}){path.suffix}")
        if not cand.exists():
            return cand
    raise ValueError(f"Não achei um nome livre para {path.name}")
