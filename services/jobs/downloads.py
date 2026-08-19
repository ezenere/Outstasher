"""qBittorrent: envio dos torrents, watchdog e troca por stall.

Problema de conexão nunca falha o job — avisa e segue tentando; torrent que
sumiu é reinserido, e download parado troca pelo próximo candidato do mesmo
corte.
"""

import asyncio
import time
from pathlib import Path

import config
from services import store
from services.jobs import runtime
from services.jobs.runtime import (
    VIDEO_EXTENSIONS, _CONN_ERRORS, _event, _map_qbit_path, _needed_torrents,
    _set, _tag, poll_interval)


async def _start_download(job: dict, a: dict | None, v: dict | None):
    if a:
        _event(job, "chosen", f"🔊 Áudio: {a['title']} (score {a['score']}, "
                              f"{a['seeders']} seeds, corte {a['edition'] or 'normal'})")
        job["audio_torrent"] = {"title": a["title"], "seeders": a["seeders"],
                                "size": a["size"], "score": a["score"], "edition": a["edition"],
                                "id": a.get("id"), "tracker": a.get("tracker")}
    if v:
        _event(job, "chosen", f"Vídeo: {v['title']} (score {v['score']}, "
                              f"{v['seeders']} seeds, corte {v['edition'] or 'normal'})")
        job["video_torrent"] = {"title": v["title"], "seeders": v["seeders"],
                                "size": v["size"], "score": v["score"], "edition": v["edition"],
                                "id": v.get("id"), "tracker": v.get("tracker")}

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
        await runtime._qbit.add(url_video, f"{_tag(job, 'video')},{_tag(job, 'audio')}", save_path)
        _event(job, "qbit", "Mesmo torrent para vídeo e áudio — adicionado uma vez")
    else:
        if v:
            await runtime._qbit.add(url_video, _tag(job, "video"), save_path)
            _event(job, "qbit", "Torrent de vídeo enviado ao qBittorrent")
        if a:
            await runtime._qbit.add(url_audio, _tag(job, "audio"), save_path)
            _event(job, "qbit", "Torrent de áudio enviado ao qBittorrent")
    _set(job, "downloading", "Baixando torrent..." if len(_needed_torrents(job)) == 1
         else "Baixando torrents...")


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
                torrents = await runtime._qbit.info_by_tag(_tag(job, kind))
                if conn_lost:
                    conn_lost = False
                    _event(job, "qbit", "Conexão com o qBittorrent reestabelecida")
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
                # normalmente só há um; se um torrent antigo sobreviveu com a
                # tag (troca que falhou pela metade), o que vale é o mais
                # recente — é sempre ele o que a troca acabou de adicionar
                t = max(torrents, key=lambda x: x.get("added_on") or 0)
                pct = t.get("progress", 0)
                # size = tamanho dos arquivos SELECIONADOS do torrent (o que vai
                # baixar de fato); total_size inclui os desmarcados. completed =
                # bytes já baixados. Sem isso a UI não mostrava o tamanho real.
                job["progress"][kind] = {
                    "pct": round(pct * 100, 1),
                    "speed": t.get("dlspeed", 0),
                    "eta": t.get("eta"),
                    "state": t.get("state"),
                    "seeds": t.get("num_seeds", 0),
                    "name": t.get("name"),
                    "size": t.get("size", 0),
                    "downloaded": t.get("completed", 0),
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
                reason = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
                _event(job, "qbit", f"Conexão com o qBittorrent perdida ({reason})")
            job["detail"] = "Sem conexão com o qBittorrent — tentando reconectar..."
        if len(paths) < len(needed):
            await asyncio.sleep(poll_interval())
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
    await runtime._qbit.add(cand.get("magnet") or cand["link"], _tag(job, kind), save_path)
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
    # tira a tag de TUDO que ainda a carrega, não só do `current` que o
    # chamador viu: se sobrar um torrent antigo com a tag, o watchdog pode
    # continuar lendo o progresso dele e a UI nunca mostra o novo
    stale = {t["hash"]: t for t in await runtime._qbit.info_by_tag(tag)}
    if current and current.get("hash"):
        stale.setdefault(current["hash"], current)
    if stale:
        # torrent que serve aos DOIS papéis (dual áudio) não é apagado: só
        # perde esta tag e continua valendo para o outro papel
        other = "audio" if kind == "video" else "video"
        shared: set[str] = set()
        if other in _needed_torrents(job):
            shared = {t.get("hash") for t in
                      await runtime._qbit.info_by_tag(_tag(job, other))}
        for h in stale:
            await runtime._qbit.remove_tag(h, tag)
            if h not in shared:
                await runtime._qbit.delete(h, delete_files=True)
    save_path = job.get("torrent_save_path") or config.QBIT_SAVE_PATH or None
    await runtime._qbit.add(nxt.get("magnet") or nxt["link"], tag, save_path)

    job[f"{kind}_torrent"] = {"title": nxt["title"], "seeders": nxt["seeders"],
                              "size": nxt["size"], "score": nxt["score"],
                              "edition": nxt["edition"]}
    if not job.get("current"):
        job["current"] = {}
    job["current"][kind] = nxt
    # o progresso do torrent que saiu não vale mais: mostra JÁ o novo em 0%
    # ("Obtendo metadados") em vez de deixar na tela o nome/percentual antigo
    # até o watchdog passar
    job["progress"][kind] = {"pct": 0.0, "speed": 0, "eta": None,
                             "state": "metaDL", "seeds": 0,
                             "name": nxt["title"], "size": nxt.get("size") or 0,
                             "downloaded": 0}
    seeds = (f"{nxt['seeders']} seeds" if nxt.get("seeders") is not None
             else "informado manualmente")
    _event(job, "qbit", f"{reason}: {nxt['title']} ({seeds})")


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
