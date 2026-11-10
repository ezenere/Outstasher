"""qBittorrent: envio dos torrents, watchdog e troca por stall.

Problema de conexão nunca falha o job — avisa e segue tentando; torrent que
sumiu é reinserido, e download parado troca pelo próximo candidato do mesmo
corte.
"""

import asyncio
import time
from pathlib import Path

import config
from services import candidates, stall_switch
from services.jobs import runtime
from services.jobs.runtime import (
    VIDEO_EXTENSIONS, _CONN_ERRORS, _event, _map_qbit_path, _needed_torrents,
    _set, _tag, poll_interval)


def _split_tags(tags: str | None) -> set[str]:
    """Campo `tags` do qBittorrent ("a, b") como conjunto."""
    return {x.strip() for x in (tags or "").split(",") if x.strip()}


async def _release_tagged(qbit, job: dict, tag: str, live=(),
                          delete_files: bool = True) -> list[dict]:
    """Solta deste job cada torrent do qBittorrent que carrega `tag` — a regra
    ÚNICA de remoção, de filme e de série: troca de torrent (manual ou do
    watchdog), edição da seleção, limpeza do fim do job e do cancel.

    O torrent só sai do qBittorrent (com os dados, se delete_files) quando não
    serve a mais ninguém. Serve ainda a ESTE job se carrega uma tag de `live`:
    o release DUAL escolhido para os dois papéis é UM torrent com as duas
    tags. Serve a OUTRO job se carrega qualquer tag que não é deste: dois jobs
    pedindo o mesmo release dividem o torrent (409 no add = a tag do outro no
    mesmo hash) — e tag posta à mão no qBittorrent também segura, na dúvida
    não se apaga dado de ninguém. Nesses casos saem só as tags deste job que
    não valem mais; a compartilhada dl-<job> fica enquanto um torrent vivo do
    job a usa (é por ela que o watchdog de série consulta). Caso real: trocar
    o pack dublado apagava do disco o pack DUAL que o original usava.

    `qbit` vem de quem chama: séries usam a referência da fachada
    (jobs._qbit), filmes a do runtime — cada lado é trocado assim nos testes.

    Devolve um resumo por torrent: {hash, name, deleted, serves, foreign} —
    serves é None quando apagado, "job" (outro papel/torrent deste job) ou
    "other" (tag de fora: foreign)."""
    base = f"dl-{job['id']}"
    live = set(live)
    out: list[dict] = []
    for info in await qbit.info_by_tag(tag):
        h = info.get("hash")
        if not h:
            continue
        tags = _split_tags(info.get("tags")) | {tag}
        ours = {x for x in tags if x == base or x.startswith(base + "-")}
        vivos = ours & live
        foreign = tags - ours
        r = {"hash": h, "name": info.get("name"), "deleted": False,
             "serves": "job" if vivos else ("other" if foreign else None),
             "foreign": sorted(foreign)}
        if r["serves"]:
            drop = ours - vivos - ({base} if vivos else set())
            if drop:
                await qbit.remove_tag(h, ",".join(sorted(drop)))
        else:
            await qbit.delete(h, delete_files=delete_files)
            r["deleted"] = True
        out.append(r)
    return out


def _why_kept(r: dict, same_job: str) -> str:
    """Por que _release_tagged deixou o torrent no qBittorrent (para o log)."""
    if r["serves"] == "job":
        return same_job
    return f"outro job ({', '.join(r['foreign'])})"


async def _drop_tagged(job: dict, kind: str):
    """Solta deste papel todo torrent que ainda carrega a tag dele — apaga o
    que não serve a OUTRO papel nem a outro job (_release_tagged). Usado
    quando o torrent do papel é substituído — pela troca durante o download
    ou por uma nova escolha depois da pausa."""
    live = {_tag(job, o) for o in _needed_torrents(job) if o != kind}
    for r in await _release_tagged(runtime._qbit, job, _tag(job, kind), live):
        if r["deleted"]:
            _event(job, "qbit", f"Torrent anterior de {kind} descartado: {r['name']}")
        else:
            _event(job, "qbit",
                   f"Torrent anterior de {kind} fica no qBittorrent — também "
                   f"serve a {_why_kept(r, 'o outro papel deste job')}: {r['name']}")


async def _start_download(job: dict, a: dict | None, v: dict | None):
    # nova escolha depois de já ter havido uma (pausa de drift, seleção manual
    # refeita): o torrent anterior de cada papel TROCADO sai do qBittorrent —
    # senão ele fica com a tag do job e o watchdog continua lendo o progresso
    # dele, e a UI nunca mostra o novo
    a = candidates.ensure(job, "audio", a) if a else None
    v = candidates.ensure(job, "video", v) if v else None
    for kind, cand in (("video", v), ("audio", a)):
        cur = candidates.active(job, kind)
        if cand and cur and cur.get("id") != cand.get("id"):
            await _drop_tagged(job, kind)

    if a:
        _event(job, "chosen", f"🔊 Áudio: {a['title']} (score {a['score']}, "
                              f"{a['seeders']} seeds, corte {a['edition'] or 'normal'})")
    if v:
        _event(job, "chosen", f"Vídeo: {v['title']} (score {v['score']}, "
                              f"{v['seeders']} seeds, corte {v['edition'] or 'normal'})")

    # reservas do mesmo corte, para o watchdog trocar se o download travar, e
    # o candidato ativo de cada papel (com magnet/link): usado para reinserir
    # se o torrent sumir do qBittorrent e para a troca manual saber o que está
    # rodando. Os dois são ATRIBUTOS do candidato — nada de copiá-lo.
    for kind, chosen in (("audio", a), ("video", v)):
        candidates.set_fallbacks(job, kind, [
            x for x in candidates.selectable(job, kind)
            if chosen and x["edition"] == chosen["edition"] and x["id"] != chosen["id"]])
        candidates.set_active(job, kind, chosen)

    _set(job, "searching", "Enviando torrents para o qBittorrent...")
    save_path = job.get("torrent_save_path") or config.QBIT_SAVE_PATH or None
    url_video = (v.get("magnet") or v["link"]) if v else None
    url_audio = (a.get("magnet") or a["link"]) if a else None

    async def enviar(url: str, tags: str, ok: str, papel: str):
        # sem ir depois das tentativas: o watchdog não o acha no qBittorrent e
        # reinsere depois da carência — um link lento não derruba o job
        erro = await runtime.send_torrent(runtime._qbit, url, tags, save_path)
        _event(job, "qbit", ok if not erro else
               f"⚠️ Torrent de {papel} não foi enviado ({erro}) — o "
               f"acompanhamento tenta de novo em alguns minutos")

    if v and a and url_video == url_audio:
        # mesmo torrent serve para os dois (ex.: release dual audio)
        await enviar(url_video, f"{_tag(job, 'video')},{_tag(job, 'audio')}",
                     "Mesmo torrent para vídeo e áudio — adicionado uma vez", "vídeo e áudio")
    else:
        if v:
            await enviar(url_video, _tag(job, "video"),
                         "Torrent de vídeo enviado ao qBittorrent", "vídeo")
        if a:
            await enviar(url_audio, _tag(job, "audio"),
                         "Torrent de áudio enviado ao qBittorrent", "áudio")
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


async def _wait_downloads(job: dict) -> dict:
    """Espera os torrents necessários terminarem; watchdog troca torrent travado.

    Perder a conexão com o qBittorrent NUNCA falha o job: avisa uma vez,
    mantém o estado "baixando" e fica tentando reconectar. Torrent que sumiu
    do qBittorrent é reinserido automaticamente — só para de reinserir se o
    job for cancelado/excluído.

    A troca automática segue a política global com o override de cada papel
    (services/stall_switch), relidos a cada tick: ligar/desligar na tela vale
    no próximo. O relógio de cada papel vai para a tela do job
    (runtime.stall_publish)."""
    try:
        return await _watch_downloads(job)
    finally:
        runtime.stall_forget(job)


async def _watch_downloads(job: dict) -> dict:
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
        policy = stall_switch.get()
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
                    runtime.stall_forget(job, kind)
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
                    runtime.stall_forget(job, kind)
                    paths[kind] = t["content_path"]
                    _event(job, "qbit", f"Download de {kind} concluído: {t['content_path']}")
                    continue

                st = stall[kind]
                state = t.get("state") or ""
                if t.get("hash") != st["hash"]:
                    # torrent trocado (watchdog ou troca manual): zera o relógio
                    st.update(hash=t.get("hash"), pct=-1.0,
                              since=time.monotonic(), warned=False)
                limit_min = stall_switch.limit_minutes(
                    policy, state, job.get(f"stall_mode_{kind}"),
                    job.get(f"stall_minutes_{kind}"))
                if state in ("stoppedDL", "pausedDL"):
                    # usuário parou o torrent manualmente: não conta o tempo de
                    # stall (o relógio recomeça do zero quando ele retomar)
                    st.update(since=time.monotonic(), warned=False)
                elif pct > st["pct"] + 1e-4:
                    st.update(pct=pct, since=time.monotonic(), warned=False)
                elif (limit_min is not None
                      and time.monotonic() - st["since"] > limit_min * 60):
                    if await _switch_torrent(job, kind, t, limit_min):
                        st.update(pct=-1.0, since=time.monotonic(), warned=False)
                    elif not st["warned"]:
                        _event(job, "qbit",
                               f"⚠️ Download de {kind} sem progresso há "
                               f"{limit_min} min e sem candidato reserva — "
                               f"continuando a esperar (cancele o job se quiser desistir)")
                        st["warned"] = True
                runtime.stall_publish(job, kind, st["since"], limit_min,
                                      paused=state in ("stoppedDL", "pausedDL"),
                                      no_backup=st["warned"])
            # persiste o progresso no banco só de tempos em tempos (eventos
            # reais persistem via _event, com a janela de EVENT_PERSIST_SECONDS)
            if time.monotonic() - last_persist >= config.PROGRESS_PERSIST_SECONDS:
                runtime._persist(job)
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
    cand = candidates.active(job, kind)
    if not cand or not (cand.get("magnet") or cand.get("link")):
        _event(job, "qbit",
               f"⚠️ Torrent de {kind} sumiu do qBittorrent e não tenho o link para "
               f"reinserir — continuando a esperar")
        return
    save_path = job.get("torrent_save_path") or config.QBIT_SAVE_PATH or None
    erro = await runtime.send_torrent(runtime._qbit, cand.get("magnet") or cand["link"],
                                      _tag(job, kind), save_path)
    if erro:
        # link da busca lento (Jackett/tracker): tenta de novo na próxima carência
        _event(job, "qbit", f"⚠️ Torrent de {kind} ainda não foi para o "
                            f"qBittorrent ({erro}) — tento de novo depois: {cand['title']}")
        return
    _event(job, "qbit",
           f"🔁 Torrent de {kind} não está mais no qBittorrent — reinserido: {cand['title']}")


async def _switch_torrent(job: dict, kind: str, current: dict, limit_min: int) -> bool:
    """Watchdog: troca um download travado pelo próximo candidato do mesmo corte."""
    reservas = candidates.fallbacks(job, kind)
    if not reservas:
        return False
    nxt = reservas[0]
    await _replace_torrent(job, kind, current, nxt,
                           f"⏳ Download de {kind} travado há {limit_min} min — trocado por")
    return True


async def _replace_torrent(job: dict, kind: str, current: dict | None, nxt: dict, reason: str):
    """Substitui o torrent ativo de `kind` por `nxt` (watchdog ou troca manual).

    `current` é o torrent como reportado pelo qBittorrent (pode ser None se ele
    nem chegou a aparecer) — quem sai de fato é definido pela TAG, não por ele.
    O candidato substituído volta para o FIM da lista de reservas — dá para
    tentar de novo mais tarde.
    """
    nxt = candidates.ensure(job, kind, nxt)
    fb = [c for c in candidates.fallbacks(job, kind) if c["id"] != nxt["id"]]
    cur_cand = candidates.active(job, kind)
    if cur_cand and cur_cand["id"] != nxt["id"]:
        fb.append(cur_cand)
    candidates.set_fallbacks(job, kind, fb)

    tag = _tag(job, kind)
    # limpa a tag inteira (não só o `current` que o chamador viu): torrent
    # antigo que sobra com a tag mantém a UI presa no download velho. Só o que
    # CARREGA a tag sai — um torrent que já a perdeu não é mais deste papel —
    # e, dos que a carregam, o que serve ao outro papel ou a outro job perde
    # só a tag
    await _drop_tagged(job, kind)
    save_path = job.get("torrent_save_path") or config.QBIT_SAVE_PATH or None
    await runtime._qbit.add(nxt.get("magnet") or nxt["link"], tag, save_path)

    candidates.set_active(job, kind, nxt)
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
        tentados = ", ".join(f"'{c}'" for c in runtime._qbit_path_candidates(job, content_path))
        raise RuntimeError(
            f"O qBittorrent reportou '{content_path}' e nenhum caminho local correspondente "
            f"existe nesta máquina (tentados: {tentados}). Se a pasta de salvamento foi "
            f"trocada no qBittorrent, cadastre em Configurações um destino de torrents com "
            f"esse caminho de salvamento e o caminho local onde ele está montado.")
    if p.is_file():
        return p
    files = [f for f in p.rglob("*")
             if f.suffix.lower() in VIDEO_EXTENSIONS and "sample" not in f.name.lower()]
    if not files:
        raise RuntimeError(f"Nenhum arquivo de vídeo encontrado em {p}")
    return max(files, key=lambda f: f.stat().st_size)
