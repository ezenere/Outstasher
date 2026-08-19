"""Merge por episódio dos jobs de série (caminho rápido).

Cada episódio baixado passa pelo MESMO fluxo dos filmes (services/merger:
GCC-PHAT em duas janelas, stream copy quando o offset é constante, hardlink
quando o melhor vídeo já tem o áudio alvo). As regras de série:

- o lock global de conversão é adquirido/liberado POR EPISÓDIO — uma
  temporada de 20 episódios não monopoliza a fila contra jobs de filme;
- falha de um episódio NÃO para os outros: marca `failed`, registra e segue;
- offsets divergentes (VersionMismatch) marcam o episódio como pendente de
  alinhamento — o motor de alinhamento por conteúdo entra na próxima etapa;
  por ora conta como falha no relatório;
- ao final: relatório; menos de 75% de sucesso entre os TENTADOS = job
  inteiro em erro (regra do produto).
"""
import asyncio
from datetime import datetime
from pathlib import Path

import config
from services import merger, store, transcode
from services import jobs
from services.series import naming, subs

# abaixo desta fração de episódios bem-sucedidos, o job inteiro falha
MIN_SUCCESS_RATIO = 0.75


async def merge_all(job: dict):
    """Converte/entrega todos os episódios `downloaded` do job, um a um."""
    keys = sorted(k for k, v in job["episodes"].items()
                  if v["state"] == "downloaded")
    if not keys:
        # ex.: revisão terminou com tudo pulado — ainda assim fecha o
        # relatório E aplica a política de limpeza dos torrents
        _finish(job)
        await _cleanup_torrents(job)
        return
    jobs._set(job, "merging",
              f"Convertendo {len(keys)} episódio(s) (um por vez)...")
    job["merge_started_at"] = datetime.now().isoformat(timespec="seconds")
    convert_opts = (transcode.validate(job["convert"])
                    if job.get("convert") else None)

    for i, key in enumerate(keys, start=1):
        ep = job["episodes"][key]
        lock = jobs._get_merge_lock()
        if lock.locked():
            job["detail"] = f"{key}: na fila de conversão ({i}/{len(keys)})..."
        async with lock:
            ep["state"] = "merging"
            job["detail"] = f"{key}: fazendo merge ({i}/{len(keys)})..."
            try:
                if ep.get("edl"):
                    # EDL pronta (revisada ou limpa): renderiza por segmentos
                    await _render_from_edl(job, key, ep)
                else:
                    await _merge_episode(job, key, ep, convert_opts)
                ep["state"] = "done"
                ep["error"] = None
                jobs._event(job, "merge", f"✅ {key} concluído: {ep['output']}")
            except asyncio.CancelledError:
                raise
            except (merger.VersionMismatch, NeedsContentAlign) as e:
                # cortes divergem (ou arquivo fundido): entra o alinhador por
                # CONTEÚDO (dHash + DP). Exceção levantada DENTRO de um handler
                # não cai no `except Exception` irmão — sem o try próprio, uma
                # falha do alinhador (ffmpeg, render...) derrubaria o job
                # inteiro em vez de falhar só este episódio
                why = (f"offsets divergentes ({e.tau1_ms:+.0f} → {e.tau2_ms:+.0f} ms)"
                       if isinstance(e, merger.VersionMismatch) else str(e))
                jobs._event(job, "merge",
                            f"{key}: {why} — rodando o alinhador por conteúdo...")
                try:
                    await _align_episode(job, key, ep)
                except asyncio.CancelledError:
                    raise
                except Exception as e2:  # noqa: BLE001 — falha por episódio
                    if job["id"] in jobs._cancelling:
                        raise asyncio.CancelledError
                    ep["state"] = "failed"
                    ep["error"] = f"{type(e2).__name__}: {e2}"
                    jobs._event(job, "merge", f"❌ {key} falhou: {ep['error']}")
                    jobs._delete_output(job)
            except Exception as e:  # noqa: BLE001 — falha por episódio, nunca do laço
                # cancelamento mata o ffmpeg e o merge levanta MergeError: isso
                # NÃO é falha do episódio — propaga o cancel (que apaga o parcial)
                if job["id"] in jobs._cancelling:
                    raise asyncio.CancelledError
                ep["state"] = "failed"
                ep["error"] = f"{type(e).__name__}: {e}"
                jobs._event(job, "merge", f"❌ {key} falhou: {ep['error']}")
                jobs._delete_output(job)  # parcial deste episódio não fica no destino
            finally:
                job["progress"]["merge"] = None
                job["output"] = None
        store.upsert_job(job)

    # episódios que pararam em revisão: o job pausa UMA vez, com todos eles
    reviews = {k: v["edl"] for k, v in job["episodes"].items()
               if v["state"] == "review"}
    if reviews:
        from services.series import pipeline
        pipeline._gate(job, "alignment_review", {"episodes": reviews},
                       f"⚠️ {len(reviews)} episódio(s) precisam de revisão de "
                       f"alinhamento")
        return
    _finish(job)
    await _cleanup_torrents(job)


async def _align_episode(job: dict, key: str, ep: dict):
    """Caminho pesado: estágios 0-4 + regras. Renderiza direto quando a EDL
    sai limpa (ou as regras resolveram tudo); senão o episódio para em
    `review` e o job pausa no gate depois do laço."""
    from services.series.align import edl as edl_mod, engine, refine, rules

    dub, orig = ep["src"]["dubbed"], ep["src"]["original"]

    log, on_progress = jobs._ffmpeg_hooks(job, jobs.runtime.PHASE_ALIGN)

    def on_wait():   # roda na thread do alinhador (como o log do ffmpeg)
        job["detail"] = f"{key}: na fila de alinhamento..."
        jobs._event(job, "merge",
                    f"{key}: outro alinhamento em andamento — na fila")

    def on_fp(info):
        # o dublado traz o ÁUDIO, o original traz o VÍDEO que fica na saída
        papel = "Áudio" if info["label"] == "dublado" else "Vídeo"
        job["detail"] = (f"{key}: buscando alinhamento ({papel}) — "
                         f"{info['pct']:.0f}%")
        on_progress(info)

    try:
        edl_dict = await asyncio.to_thread(
            engine.align_pair, dub, orig, key, on_wait=on_wait,
            on_progress=on_fp)
    except engine.AlignConflict as e:
        # o PAR está errado (ordem trocada, episódios fundidos): não há o que
        # alinhar — falha deste episódio com o diagnóstico, o job segue
        ep["state"] = "failed"
        ep["error"] = f"conflito de alinhamento: {e}"
        jobs._event(job, "merge", f"⚠️ {key}: {ep['error']}")
        return

    segs = edl_mod.segments(edl_dict)
    # ffprobe é bloqueante (subprocess): fora do event loop, como o resto.
    # Para MEDIR usa o par de mesma língua (faixa inglesa do dual áudio vs
    # original): correlação bem mais nítida do que dublagem vs original
    dub_a, orig_a = await asyncio.to_thread(_alignment_pair, dub, orig)
    job["progress"]["merge"] = None      # fingerprint terminou: some a barra
    segs = await asyncio.to_thread(
        refine.refine_offsets, segs, dub, dub_a, orig, orig_a, log)
    dur_a = edl_dict["source_dub"]["duration"]
    segs, needs = rules.apply_rules(segs, job.get("review_rules") or [], dur_a)
    # o rebuild só reescreve os segmentos: os metadados do arquivo FUNDIDO
    # (janela/nota) têm que sobreviver — o render corta o original por eles
    extras = {k: edl_dict[k] for k in ("merged_side", "a_window", "b_window", "note")
              if k in edl_dict}
    edl_dict = edl_mod.build(segs, key, dub, dur_a, orig,
                             edl_dict["source_orig"]["duration"],
                             profile=edl_dict.get("confidence_profile"))
    edl_dict.update(extras)
    # review.required do build considera todo `replaced`; com ação (explícita
    # ou por regra) a revisão está RESOLVIDA
    edl_dict["review"]["required"] = needs
    ep["edl"] = edl_dict
    if needs:
        ep["state"] = "review"
        jobs._event(job, "merge",
                    f"🔍 {key}: cena(s) substituída(s) sem decisão — revisão "
                    f"humana necessária")
        return
    await _render_from_edl(job, key, ep)
    ep["state"] = "done"
    ep["error"] = None
    jobs._event(job, "merge", f"✅ {key} concluído (EDL): {ep['output']}")


def _alignment_pair(dub_path: str, orig_path: str) -> tuple[int, int]:
    """(a:N do dublado, a:N do original) para MEDIR offset — de preferência a
    mesma língua nos dois (o dual áudio BR costuma trazer a faixa inglesa,
    idêntica à do original: pico de correlação limpo)."""
    probes = [merger.ffprobe_json(orig_path), merger.ffprobe_json(dub_path)]
    for pr in probes:
        merger.annotate_type_indexes(pr)
    orig_a, dub_a = merger.choose_alignment_pair(probes, 0)
    return dub_a, orig_a


def _audio_indexes(dub_path: str, orig_path: str,
                   target_lang: str) -> tuple[int, int, int]:
    """(índice a:N do dublado, do original, canais do dublado) — a mesma
    seleção do renderer, exposta para o refino usar."""
    probe_dub = merger.ffprobe_json(dub_path)
    probe_orig = merger.ffprobe_json(orig_path)
    merger.annotate_type_indexes(probe_dub)
    merger.annotate_type_indexes(probe_orig)
    iso = merger.canonical_lang(target_lang)
    best_dub = merger.choose_best_audio_per_language([probe_dub], {0: iso})
    ds = (best_dub.get(iso) or best_dub.get("und")
          or next(iter(best_dub.values()), None))
    if ds is None:
        raise merger.MergeError(f"nenhum áudio no arquivo dublado ({dub_path})")
    best_orig = merger.choose_best_audio_per_language([probe_orig], {})
    os_ = next((v for k, v in best_orig.items() if k != iso),
               next(iter(best_orig.values()), None))
    if os_ is None:
        raise merger.MergeError(f"nenhum áudio no arquivo original ({orig_path})")
    return (ds[1]["_type_index"], os_[1]["_type_index"],
            int(ds[1].get("channels") or 2))


async def _render_from_edl(job: dict, key: str, ep: dict):
    """Renderiza a EDL do episódio no destino (timeline do original)."""
    from services.series.align import edl as edl_mod, render as render_mod

    if job.get("convert"):
        jobs._event(job, "merge",
                    f"{key}: opções avançadas de conversão ainda não se "
                    f"aplicam ao caminho de EDL — saída em stream copy")
    m = job["movie"]
    folder = naming.series_folder_name(m["original_title"], m["year"],
                                       job.get("tmdb_id"))
    stem = naming.episode_file_name(m["original_title"], m["year"],
                                    ep["season"], ep["episode"],
                                    job["language"])
    dest_dir = Path(job.get("destination_path") or config.OUTPUT_DIR)
    output = dest_dir / folder / naming.season_dir_name(ep["season"]) / f"{stem}.mkv"
    job["output"] = str(output)
    segs = edl_mod.segments(ep["edl"])
    # o passo 1 do render monta a faixa dublada (arquivo intermediário): é a
    # etapa que a UI chama de "Gerando áudio da EDL", não de conversão
    log, on_progress = jobs._ffmpeg_hooks(job, jobs.runtime.PHASE_EDL)
    if ep["edl"].get("note"):
        log(ep["edl"]["note"])
    try:
        info = await asyncio.to_thread(
            render_mod.render, segs, ep["src"]["dubbed"],
            ep["src"]["original"], str(output), job["language"],
            log, on_progress, jobs._register_proc(job["id"]),
            ep["edl"].get("b_window"))
    finally:
        jobs._ffmpeg_procs.pop(job["id"], None)
    ep["output"] = str(output)
    # legendas externas: as do original só deslocam pela janela; as do
    # dublado seguem a EDL (mesmos cortes que o áudio dublado)
    b_shift = float((info or {}).get("b_shift") or 0.0)
    await _attach_subs(
        job, key, ep, str(output),
        orig_fn=subs.shift_fn(-b_shift),
        dub_fn=subs.edl_fn(ep["edl"].get("segments") or [], b_shift), log=log)


class NeedsContentAlign(Exception):
    """O par não serve para o caminho rápido (offset escalar) — vai direto
    para o alinhador por conteúdo."""


async def _merge_episode(job: dict, key: str, ep: dict, convert_opts):
    """Um episódio: mesmo fluxo do merge de filmes, com o nome de série."""
    m = job["movie"]
    # arquivo FUNDIDO (razão de duração ~2): o offset escalar do caminho
    # rápido não tem como estar certo — e a validação em duas janelas pode
    # até "concordar" por acaso e entregar lixo. Vai direto para o alinhador.
    from services.series.align import classify as _cls, engine as _eng
    d_a, d_b = await asyncio.gather(
        asyncio.to_thread(_eng._duration, ep["src"]["dubbed"]),
        asyncio.to_thread(_eng._duration, ep["src"]["original"]))
    if _cls.check_duration_ratio(d_a, d_b):
        raise NeedsContentAlign(
            f"durações {d_a:.0f}s vs {d_b:.0f}s — caminho rápido não se aplica")
    # o merge de filmes valida o offset em DUAS janelas (0:30 e ~60%); em série
    # isso deixa passar junções de intervalo comercial (2 frames = 68 ms) e
    # cenas cortadas no meio — caso real de campo (S01E01). Aqui: uma janela a
    # cada 5 min, tolerância de lip sync (50 ms); qualquer desvio -> alinhador
    # por conteúdo, que corta no silêncio certo em vez de aplicar um offset só
    from services.series.align import refine as _ref
    dub_a, orig_a = await asyncio.to_thread(
        _alignment_pair, ep["src"]["dubbed"], ep["src"]["original"])
    constant, pts = await asyncio.to_thread(
        _ref.scan_constant_offset, ep["src"]["dubbed"], dub_a,
        ep["src"]["original"], orig_a, min(d_a, d_b))
    if not constant:
        offs = ", ".join(f"{t / 60:.0f}min {o * 1000:+.0f}ms" for t, o, _ in pts)
        raise NeedsContentAlign(f"offset varia ao longo do episódio ({offs})")
    if pts:
        jobs._event(job, "merge",
                    f"{key}: offset constante em {len(pts)} janela(s) "
                    f"({pts[0][1] * 1000:+.0f} ms) — caminho rápido")
    folder = naming.series_folder_name(m["original_title"], m["year"],
                                       job.get("tmdb_id"))
    season_dir = naming.season_dir_name(ep["season"])
    stem = naming.episode_file_name(m["original_title"], m["year"],
                                    ep["season"], ep["episode"],
                                    job["language"])
    dest_dir = Path(job.get("destination_path") or config.OUTPUT_DIR)
    output = dest_dir / folder / season_dir / f"{stem}.mkv"
    # registra o destino JÁ: cancelar durante o ffmpeg precisa apagar o parcial
    job["output"] = str(output)

    log, on_progress = jobs._ffmpeg_hooks(job)
    try:
        result = await asyncio.to_thread(
            merger.merge, ep["src"]["original"], ep["src"]["dubbed"],
            str(output), job["language"], log=log, on_progress=on_progress,
            allow_drift=False, convert=convert_opts,
            original_lang=m.get("original_language"),
            on_start=jobs._register_proc(job["id"]))
    finally:
        jobs._ffmpeg_procs.pop(job["id"], None)
    ep["output"] = result.output
    # legendas externas: offset constante do container (por input); se o
    # merge foi pulado (hardlink), só o lado que virou a saída tem tempo
    # conhecido
    if result.linked:
        ref = result.ref_input if result.ref_input is not None else 0
        orig_fn = subs.shift_fn(0.0) if ref == 0 else None
        dub_fn = subs.shift_fn(0.0) if ref == 1 else None
    else:
        orig_fn = subs.shift_fn(result.input_shifts[0])
        dub_fn = subs.shift_fn(result.input_shifts[1])
    await _attach_subs(job, key, ep, result.output, orig_fn, dub_fn, log=log)


async def _attach_subs(job: dict, key: str, ep: dict, output: str,
                       orig_fn, dub_fn, log=print):
    """Muxa as legendas externas do episódio no arquivo entregue. Nunca
    derruba o episódio: legenda é acessório."""
    ext = ep.get("subs") or {}
    orig_subs = [p for p in ext.get("original") or [] if Path(p).exists()]
    dub_subs = [p for p in ext.get("dubbed") or [] if Path(p).exists()]
    if not orig_subs and not dub_subs:
        return
    m = job.get("movie") or {}
    orig_lang = merger.canonical_lang(merger.LANG_ISO.get(
        m.get("original_language") or "", m.get("original_language") or "und"))
    dub_lang = merger.canonical_lang(merger.LANG_ISO.get(
        job["language"], job["language"]))
    try:
        n = await asyncio.to_thread(
            subs.attach, output, orig_subs, dub_subs, orig_fn, dub_fn,
            ep["src"].get("original"), ep["src"].get("dubbed"),
            orig_lang, dub_lang, log)
        if n:
            jobs._event(job, "merge", f"{key}: {n} legenda(s) externa(s) anexada(s)")
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001
        jobs._event(job, "merge", f"⚠️ {key}: legendas externas não anexadas ({e})")


def _finish(job: dict):
    """Relatório final + regra dos 75%: só os episódios TENTADOS contam
    (skipped_* ficam fora do denominador — foram decisão, não falha)."""
    done = [k for k, v in job["episodes"].items() if v["state"] == "done"]
    failed = sorted(k for k, v in job["episodes"].items()
                    if v["state"] in ("failed", "review"))
    skipped = sorted(k for k, v in job["episodes"].items()
                     if v["state"] in ("skipped_future", "skipped_missing"))
    attempted = len(done) + len(failed)
    job["report"] = {"attempted": attempted, "succeeded": len(done),
                     "failed": failed, "skipped": skipped}
    job["output"] = None

    if attempted == 0:
        jobs._fail(job, "Nenhum episódio chegou à conversão")
        return
    ratio = len(done) / attempted
    summary = (f"{len(done)}/{attempted} episódio(s) concluído(s)"
               + (f", {len(failed)} falha(s)" if failed else "")
               + (f", {len(skipped)} pulado(s)" if skipped else ""))
    if ratio < MIN_SUCCESS_RATIO:
        # menos de 75% de sucesso: o resultado não é confiável como um todo
        jobs._fail(job, f"Abortado: só {summary} (< 75%) — use ↻ ou crie um "
                        f"job novo para os episódios que falharam")
        return
    jobs._set(job, "done", f"Concluído: {summary}"
              + (" — crie um job novo para os que falharam" if failed else ""))


async def _cleanup_torrents(job: dict):
    """Limpeza pós-merge conforme QBIT_CLEANUP (keep/remove/remove_data) —
    mesma política dos filmes, pela tag compartilhada do job."""
    if config.QBIT_CLEANUP == "keep":
        return
    delete_files = config.QBIT_CLEANUP == "remove_data"
    from services.series.pipeline import shared_tag
    try:
        await jobs._qbit.delete_by_tag(shared_tag(job), delete_files)
        jobs._event(job, "qbit", "Torrents do job removidos do qBittorrent"
                    + (" (com os dados)" if delete_files else ""))
    except Exception as e:  # noqa: BLE001
        jobs._event(job, "qbit", f"Falha ao remover torrents: {e}")
