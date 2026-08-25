"""Alinhamento avançado dos filmes (EDL por conteúdo).

Quando o offset diverge entre as janelas, o job para e oferece o alinhador das
séries: perfil do offset (drift x corte), EDL, revisão humana e render por
segmentos.
"""

import asyncio
from datetime import datetime
from pathlib import Path

from services import merger, store
from services.series import subs as ext_subs
from services.jobs import delivery, runtime
from services.jobs.runtime import (
    _event, _fail, _ffmpeg_hooks, _ffmpeg_procs, _get_merge_lock, _jobs,
    _map_qbit_path, _public, _register_proc, _set, _spawn)


def _offset_profile(video_file: str, audio_file: str) -> tuple[list[dict], str]:
    """Offset medido a cada 5 min ao longo do filme (mesmo scanner das séries)
    e um veredito de FORMA: 'drift' (muda aos poucos, monotônico), 'cut'
    (patamares com salto), 'mixed' ou 'flat'. Duas janelas não distinguem
    drift de corte; o perfil inteiro sim. Bloqueante (ffmpeg)."""
    from services.series.align import refine
    probes = [merger.ffprobe_json(video_file), merger.ffprobe_json(audio_file)]
    for pr in probes:
        merger.annotate_type_indexes(pr)
    ref_a, oth_a = merger.choose_alignment_pair(probes, 0)
    dur = min(merger._duration_of(pr) or 0.0 for pr in probes)
    _, pts = refine.scan_constant_offset(audio_file, oth_a, video_file, ref_a, dur)
    profile = [{"t": round(t, 1), "offset_ms": round(o * 1000, 1), "quality": round(q, 1)}
               for t, o, q in pts]
    verdict = _shape_verdict([o for _, o, _ in pts])
    return profile, verdict


def _shape_verdict(offs: list[float]) -> str:
    if len(offs) < 3:
        return "unknown"
    span = max(offs) - min(offs)
    if span <= 0.05:
        return "flat"
    steps = [b - a for a, b in zip(offs, offs[1:])]
    big = [d for d in steps if abs(d) >= 0.10]      # salto = corte
    small = [d for d in steps if abs(d) < 0.10]
    if not big:
        # tudo pequeno e no mesmo sentido: velocidade/frame rate (drift)
        signs = {1 if d > 0 else -1 for d in small if abs(d) > 0.005}
        return "drift" if len(signs) <= 1 else "mixed"
    # saltos raros entre patamares estáveis = corte
    if len(big) <= max(3, len(steps) // 4) and all(abs(d) < 0.03 for d in small):
        return "cut"
    return "mixed"


async def _pause_for_drift(job: dict, files: dict, e: "merger.VersionMismatch"):
    """Conversão abortada antes do ffmpeg pesado: as duas janelas de offset
    divergem, então o áudio provavelmente é de outro corte/versão e o resultado
    sairia dessincronizado. Em vez de gastar CPU à toa, o job para em
    'awaiting' (bolinha vermelha de resposta pendente) e o usuário decide:
    Continuar mesmo assim (proceed), rodar o alinhamento avançado (o mesmo
    das séries: EDL por conteúdo), escolher outro torrent ou cancelar.

    Antes de parar, mede o offset a cada 5 min — o perfil inteiro diz se é
    drift (muda aos poucos) ou corte (salto entre patamares)."""
    job["progress"]["merge"] = None
    gate = {
        "video_file": str(files["video"]), "audio_file": str(files["audio"]),
        "tau1_ms": e.tau1_ms, "tau2_ms": e.tau2_ms,
    }
    job["drift_confirm"] = gate
    _set(job, "awaiting",
         f"⚠️ Possível versão/corte diferente (offset {e.tau1_ms:+.0f} → "
         f"{e.tau2_ms:+.0f} ms). Medindo o perfil do offset...")
    try:
        profile, verdict = await asyncio.to_thread(
            _offset_profile, str(files["video"]), str(files["audio"]))
        gate["profile"] = profile
        gate["verdict"] = verdict
        label = {"drift": "drift (muda aos poucos)", "cut": "corte (salto entre patamares)",
                 "flat": "constante nas janelas medidas", "mixed": "misto"}.get(verdict, "indefinido")
        _event(job, "merge", f"Perfil do offset em {len(profile)} janela(s): {label} — "
               + ", ".join(f"{p['t'] / 60:.0f}min {p['offset_ms']:+.0f}ms" for p in profile[:12])
               + ("…" if len(profile) > 12 else ""))
    except asyncio.CancelledError:
        raise
    except Exception as ex:  # noqa: BLE001 — o perfil é informativo
        _event(job, "merge", f"⚠️ perfil do offset não medido ({ex})")
    # o usuário pode ter DECIDIDO durante a medição (proceed/troca de torrent):
    # o gate já saiu do job e o status é do fluxo novo — reescrever "awaiting"
    # aqui atropelava o alinhamento em andamento (caso real de campo: status
    # "awaiting" com o fingerprint rodando, e um segundo proceed aceito)
    if job.get("drift_confirm") is gate:
        _set(job, "awaiting",
             f"⚠️ Possível versão/corte diferente (offset {e.tau1_ms:+.0f} → "
             f"{e.tau2_ms:+.0f} ms). Conversão pausada.")


async def proceed(job_id: str, mode: str = "offset") -> dict | None:
    """Após a pausa de drift: mode='offset' converte com o offset do início
    (comportamento antigo); mode='advanced' roda o alinhador por conteúdo das
    séries (EDL) — para o caso de mesmo corte com uma cena/junção diferente."""
    job = _jobs.get(job_id)
    if not job or job["status"] != "awaiting" or not job.get("drift_confirm"):
        return None
    info = job.pop("drift_confirm")
    vf, af = Path(info["video_file"]), Path(info["audio_file"])
    if mode == "advanced":
        _event(job, "chosen", "Usuário pediu o alinhamento avançado (por conteúdo)")
        _spawn(job["id"], _run_advanced(job, vf, af))
    else:
        _event(job, "chosen", "Usuário mandou converter mesmo com offsets divergentes")
        _spawn(job["id"], _resume_merge(job, vf, af))
    return _public(job)


async def _run_advanced(job: dict, video_file: Path, audio_file: Path):
    """Alinhador por conteúdo das séries aplicado ao filme: EDL, revisão
    (mesmo gate/UI das séries) e render por segmentos."""
    from services.series.align import edl as edl_mod, engine, refine, rules
    from services.series.merge_runner import _alignment_pair
    try:
        _set(job, "merging", "Buscando alinhamento: lendo os dois arquivos...")
        job["merge_started_at"] = datetime.now().isoformat(timespec="seconds")
        dub, orig = str(audio_file), str(video_file)

        log, on_progress = _ffmpeg_hooks(job, runtime.PHASE_ALIGN)

        def on_wait():   # roda na thread do alinhador (como o log do ffmpeg)
            job["detail"] = "Alinhamento avançado: na fila..."
            _event(job, "merge", "Outro alinhamento em andamento — na fila")

        def on_fp(info):
            # o dublado traz o ÁUDIO, o original traz o VÍDEO da saída
            papel = "Áudio" if info["label"] == "dublado" else "Vídeo"
            job["detail"] = (f"Buscando alinhamento ({papel}) — "
                             f"{info['pct']:.0f}%")
            on_progress(info)

        edl_dict = await asyncio.to_thread(engine.align_pair, dub, orig, "filme",
                                           on_wait=on_wait, on_progress=on_fp)
        segs = edl_mod.segments(edl_dict)
        dub_a, orig_a = await asyncio.to_thread(_alignment_pair, dub, orig)
        job["progress"]["merge"] = None      # fingerprint terminou: some a barra
        job["detail"] = "Alinhamento avançado: refino por áudio..."
        segs = await asyncio.to_thread(
            refine.refine_offsets, segs, dub, dub_a, orig, orig_a, log)
        dur_a = edl_dict["source_dub"]["duration"]
        segs, needs = rules.apply_rules(segs, job.get("review_rules") or [], dur_a)
        extras = {k: edl_dict[k] for k in ("merged_side", "a_window", "b_window", "note")
                  if k in edl_dict}
        edl_dict = edl_mod.build(segs, "filme", dub, dur_a, orig,
                                 edl_dict["source_orig"]["duration"],
                                 profile=edl_dict.get("confidence_profile"))
        edl_dict.update(extras)
        edl_dict["review"]["required"] = needs
        job["advanced"] = {"video_file": orig, "audio_file": dub, "edl": edl_dict}
        st = edl_mod.stats(edl_dict)
        _event(job, "merge", f"EDL: {st.get('match_pct', 0):.0f}% casado, "
                             f"{len(edl_dict['segments'])} segmento(s)")
        if needs:
            job["awaiting"] = {"reason": "alignment_review",
                               "payload": {"episodes": {"filme": edl_dict}}}
            _set(job, "awaiting", "⚠️ Cena(s) substituída(s) sem decisão — "
                                  "revisão de alinhamento necessária")
            return
        await _render_advanced(job)
    except asyncio.CancelledError:
        raise
    except engine.AlignConflict as e:
        _fail(job, f"conflito de alinhamento: {e}")
    except Exception as e:  # noqa: BLE001
        _fail(job, f"{type(e).__name__}: {e}")


async def _render_advanced(job: dict):
    """Renderiza a EDL do filme (job['advanced']) no destino."""
    from services.series.align import edl as edl_mod, render as render_mod
    adv = job["advanced"]
    output = delivery._movie_output(job)
    job["output"] = str(output)
    lock = _get_merge_lock()
    if lock.locked():
        _set(job, "merging", "Na fila de conversão...")
    async with lock:
        _set(job, "merging", "Renderizando a EDL...")
        segs = edl_mod.segments(adv["edl"])
        # passo 1 do render: faixa dublada remontada (arquivo intermediário)
        log, on_progress = _ffmpeg_hooks(job, runtime.PHASE_EDL)
        if adv["edl"].get("note"):
            log(adv["edl"]["note"])
        # legendas externas dos DOIS torrents entram no próprio mux do render
        # (o do original com a janela, o do dublado pela EDL): anexar depois
        # reescrevia o filme inteiro — num REMUX, dezenas de GB de novo
        def _achar(lado: str, arquivo: str) -> list[str]:
            roots = job.get("src_roots") or {}
            try:
                raiz = _map_qbit_path(job, roots[lado]) if roots.get(lado) \
                    else Path(arquivo).parent
            except Exception:  # noqa: BLE001
                raiz = Path(arquivo).parent
            return [str(p) for p in ext_subs.find_for_movie(raiz, arquivo)]

        movie = job.get("movie") or {}
        v_subs, a_subs = await asyncio.gather(
            asyncio.to_thread(_achar, "video", adv["video_file"]),
            asyncio.to_thread(_achar, "audio", adv["audio_file"]))
        externas = {
            "orig": v_subs, "dub": a_subs,
            "orig_video": adv["video_file"], "dub_video": adv["audio_file"],
            "orig_lang": merger.canonical_lang(merger.LANG_ISO.get(
                movie.get("original_language") or "",
                movie.get("original_language") or "und")),
            "dub_lang": merger.canonical_lang(merger.LANG_ISO.get(
                job["language"], job["language"])),
        }
        try:
            info = await asyncio.to_thread(
                render_mod.render, segs, adv["audio_file"], adv["video_file"],
                str(output), job["language"], log, on_progress,
                _register_proc(job["id"]), adv["edl"].get("b_window"),
                externas)
        finally:
            _ffmpeg_procs.pop(job["id"], None)
        job["progress"]["merge"] = None
        if v_subs or a_subs:
            _event(job, "merge",
                   f"{(info or {}).get('subs_muxed') or 0} legenda(s) "
                   f"externa(s) no mux final")
    job["output"] = str(output)
    _set(job, "done", f"Concluído (alinhamento avançado): {output}")
    await delivery._cleanup_torrents(job)


async def resolve_review(job_id: str, reason: str, decision: dict) -> dict | None:
    """Gate de revisão de alinhamento num job de FILME (mesmo formato de
    decisão da UI das séries: actions {'filme': {idx: ação}}, rules, skip)."""
    from services.series.align import edl as edl_mod, rules as rules_mod
    job = _jobs.get(job_id)
    if not job or job.get("media_type") == "tv":
        return None
    gate = job.get("awaiting")
    if job["status"] != "awaiting" or not gate or gate.get("reason") != reason \
            or reason != "alignment_review" or not job.get("advanced"):
        raise ValueError("O job não está aguardando revisão de alinhamento")
    if "filme" in set(decision.get("skip") or []):
        job.pop("awaiting", None)
        _fail(job, "revisão de alinhamento pulada pelo usuário")
        return _public(job)
    new_rules = rules_mod.validate_rules(decision.get("rules") or [])
    if new_rules:
        job["review_rules"] = (job.get("review_rules") or []) + new_rules
    edl_dict = job["advanced"]["edl"]
    segs_raw = edl_dict.get("segments") or []
    for idx_str, action in ((decision.get("actions") or {}).get("filme") or {}).items():
        idx = int(idx_str)
        if not 0 <= idx < len(segs_raw):
            raise ValueError(f"segmento {idx} não existe")
        if action not in rules_mod.VALID_ACTIONS:
            raise ValueError(f"ação inválida {action!r}")
        segs_raw[idx]["action"] = action
    segs = edl_mod.segments(edl_dict)
    dur_a = edl_dict.get("source_dub", {}).get("duration", 0.0)
    segs, needs = rules_mod.apply_rules(segs, job.get("review_rules") or [], dur_a)
    for raw, seg in zip(segs_raw, segs):
        if seg.extra.get("action"):
            raw["action"] = seg.extra["action"]
    edl_dict["review"]["required"] = needs
    if needs:
        job["awaiting"]["payload"] = {"episodes": {"filme": edl_dict}}
        store.upsert_job(job)
        return _public(job)
    job.pop("awaiting", None)
    _event(job, "chosen", "Revisão de alinhamento resolvida")
    _spawn(job["id"], _render_advanced(job))
    return _public(job)


async def _resume_merge(job: dict, video_file: Path, audio_file: Path):
    """Retoma o merge pausado pelo drift, agora com allow_drift=True (a medição
    das janelas se repete, mas isso custa segundos — o caro é o re-encode)."""
    try:
        lock = _get_merge_lock()
        if lock.locked():
            _set(job, "merging", "Na fila de conversão...")
        async with lock:
            await delivery._merge(job, video_file, audio_file, allow_drift=True)
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001
        _fail(job, f"{type(e).__name__}: {e}")
