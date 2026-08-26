"""Estágio 7: renderização da EDL — merge por segmentos de verdade.

A timeline de saída é a do ORIGINAL (o vídeo que fica): vídeo, legendas e os
áudios originais passam em STREAM COPY; só a faixa dublada é remontada fatia
a fatia (atrim/asetpts/concat) seguindo a EDL:

- match/drift: fatia do dublado deslocada pelo offset refinado;
- pal: fatia do dublado com rubberband (tempo+pitch juntos; fallback
  asetrate+aresample+atempo quando o build do ffmpeg não tem rubberband);
- gap_orig: preenchido com o ÁUDIO ORIGINAL por padrão (silêncio é pior
  experiência que uma frase em inglês — e o usuário percebe que não é bug),
  ou silêncio se a revisão mandou;
- replaced: a ação vem da revisão (fill_original / use_dub / silence);
- gap_dub: descartado — o material não existe no vídeo final.

Os CAPÍTULOS do original são preservados como estão (é a timeline dele);
nenhum capítulo de auditoria é criado — os preenchimentos ficam registrados
na EDL do job, não na mídia. Num original FUNDIDO (b_window) o ffmpeg desloca
e recorta os capítulos junto com o -ss/-to.
"""
import re
import subprocess
import tempfile
import threading
from functools import lru_cache
from pathlib import Path

from services import merger
from services.series.align.classify import Segment

# tolerância para "fatia colada na anterior" (evita micro-gaps de arredondamento)
_EPS = 0.02
# buraco entre duas fatias de dublagem até isto é arredondamento (grade de
# 0,25 s do vídeo), não falta de dublagem: a fatia anterior estende
SMALL_HOLE_S = 0.35


@lru_cache(maxsize=1)
def has_rubberband() -> bool:
    """O build do ffmpeg tem o filtro rubberband? (probe único, cacheado)"""
    try:
        p = subprocess.run(["ffmpeg", "-hide_banner", "-filters"],
                           capture_output=True, text=True, timeout=30)
        return " rubberband " in p.stdout
    except (OSError, subprocess.TimeoutExpired):
        return False


def _layout(channels: int) -> str:
    return {1: "mono", 2: "stereo", 6: "5.1", 8: "7.1"}.get(channels, "stereo")


def _keyframe_at_or_before(path: str, t: float) -> float | None:
    """Maior keyframe de vídeo <= t (procura em janelas crescentes para trás).
    None se não achar — o chamador decide o fallback."""
    for back in (10.0, 30.0, 90.0):
        lo = max(0.0, t - back)
        p = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-skip_frame", "nokey", "-show_entries", "frame=pts_time",
             "-of", "csv=p=0", "-read_intervals", f"{lo:.3f}%{t + 0.5:.3f}",
             path], capture_output=True, text=True, timeout=120)
        kfs = []
        for line in p.stdout.splitlines():
            try:
                v = float(line.strip().split(",")[0])
            except ValueError:
                continue
            if v <= t + 1e-3:
                kfs.append(v)
        if kfs:
            return max(kfs)
        if lo == 0.0:
            break
    return None


def _keyframe_at_or_after(path: str, t: float) -> float | None:
    """Menor keyframe de vídeo >= t (janelas crescentes para frente)."""
    for ahead in (10.0, 30.0, 90.0):
        p = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-skip_frame", "nokey", "-show_entries", "frame=pts_time",
             "-of", "csv=p=0", "-read_intervals", f"{max(0.0, t - 0.5):.3f}%{t + ahead:.3f}",
             path], capture_output=True, text=True, timeout=120)
        kfs = []
        for line in p.stdout.splitlines():
            try:
                v = float(line.strip().split(",")[0])
            except ValueError:
                continue
            if v >= t - 1e-3:
                kfs.append(v)
        if kfs:
            return min(kfs)
    return None


def _reencode_for_cuts(orig_path: str, tmp_dir: Path, times: list[float],
                       spec: dict, duration: float, log, on_progress=None,
                       on_start=None) -> str:
    """Re-encoda o VÍDEO do original com keyframe forçado em cada ponto de
    corte (áudios/legendas em cópia): o corte por stream copy vira exato no
    frame. É o preço de não ter raspas (até um GOP de cena sem dublagem)
    nem mudo/crack nas junções. spec: {"codec": "av1", "crf": 20,
    "preset": "default"}."""
    from services import transcode
    codec = spec.get("codec", "av1")
    crf = int(spec.get("crf", 20))
    enc = spec.get("encoder") or "auto"
    if enc == "auto":
        # GPU Intel (Arc) faz AV1 a ~8× tempo real; o SVT-AV1 em CPU leva
        # horas por episódio (e já travou em deadlock com 90+ threads num
        # caso real). Hardware quando existe, software só de fallback.
        enc = "av1_qsv" if (codec == "av1" and transcode.hw_encoder_works("av1_qsv")) \
            else ("libsvtav1" if codec == "av1" else "libx264")
    pre_in: list[str] = []
    args = ["-c:v", enc]
    if enc.endswith("_qsv"):
        # decode por VAAPI (o decoder QSV das Arc descarta frames em silêncio
        # — ver DECODE_QSV.md), frames pela RAM, encode QSV
        pre_in = ["-hwaccel", "vaapi", "-hwaccel_device", transcode.VAAPI_RENDER_NODE]
        if transcode._hw_probe(enc) == "low_power":
            args += ["-low_power", "1"]
        args += ["-global_quality", str(crf), "-preset", "veryslow",
                 "-pix_fmt", "nv12"]
    elif enc == "libsvtav1":
        preset = transcode._PRESETS[enc].get(spec.get("preset", "default"), "6")
        args += ["-preset", preset, "-crf", str(crf)]
        la = transcode.svtav1_lookahead()
        if la is not None:
            args += ["-svtav1-params", f"lookahead={la}"]
    else:
        args += ["-crf", str(crf)]
    probe = merger.ffprobe_json(orig_path)
    fps = 24.0
    for st in merger.get_streams(probe, "video"):
        try:
            n, d = st.get("r_frame_rate", "24/1").split("/")
            fps = float(n) / float(d)
        except (ValueError, ZeroDivisionError):
            pass
        break
    args += ["-g", str(int(round(fps * transcode.GOP_SECONDS)))]
    kfs = ",".join(f"{t:.3f}" for t in sorted(set(times)))
    out = tmp_dir / "orig_reenc.mkv"
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
           "-progress", "pipe:1", *pre_in, "-i", orig_path, "-map", "0",
           "-c", "copy", *args, "-force_key_frames", kfs, str(out)]
    log(f"Re-encodando o vídeo em {codec.upper()} ({enc}, qualidade {crf}) com "
        f"keyframe forçado em {len(set(times))} ponto(s) de corte...")
    merger._run_ffmpeg_progress(cmd, duration, on_progress, on_start)
    return str(out)


def _apply_video_cuts(segs, orig_path: str, tmp_dir: Path, log,
                      reencode: dict | None = None, on_progress=None,
                      on_start=None):
    """Ação cut_video: remove do ORIGINAL os trechos b marcados, por stream
    copy (mkvmerge --split parts:, que também reajusta legendas embutidas e
    capítulos). Cortes caem em KEYFRAMES: as raspas de até um GOP que sobram
    nas bordas ficam como gap_orig minúsculo (áudio original), nada é
    recodificado.

    Retorna (segs_remapeados, caminho_do_cortado | None, cortes_reais).
    Sem mkvmerge, loga e devolve tudo intacto (os trechos caem no
    preenchimento padrão)."""
    alvos = [seg for seg in segs
             if seg.extra.get("action") == "cut_video"
             and seg.kind in ("gap_orig", "replaced")
             and (seg.b_end or 0) - (seg.b_start or 0) >= 1.0]
    if not alvos:
        return segs, None, []
    if not has_mkvmerge():
        log("⚠️ cut_video pedido mas não há mkvmerge — trechos ficam com "
            "áudio original")
        return segs, None, []

    # junção com dublado CONTÍNUO (refine guardou junction_a): o corte vai
    # exatamente onde o dublado pula, e os dois matches encostam nesse ponto
    # — o áudio dublado fica sem emenda nenhuma
    for idx, seg in enumerate(segs):
        ja = seg.extra.get("junction_a")
        if seg not in alvos or ja is None:
            continue
        prev = next((segs[k] for k in range(idx - 1, -1, -1)
                     if segs[k].kind in ("match", "drift")), None)
        nxt = next((segs[k] for k in range(idx + 1, len(segs))
                    if segs[k].kind in ("match", "drift")), None)
        if prev is None or nxt is None or prev.offset is None or nxt.offset is None:
            continue
        b0, b1 = ja + prev.offset, ja + nxt.offset
        if b1 - b0 < 0.5 or ja <= prev.a_start + 0.5 or ja >= nxt.a_end - 0.5:
            continue
        prev.a_end, prev.b_end = ja, b0
        nxt.a_start, nxt.b_start = ja, b1
        seg.a_start = seg.a_end = ja
        seg.b_start, seg.b_end = b0, b1

    dur = float(merger.ffprobe_json(orig_path)["format"]["duration"])
    if reencode:
        pontos = [t for seg in alvos for t in (seg.b_start, seg.b_end)
                  if 0.5 < t < dur - 0.5]
        orig_path = _reencode_for_cuts(orig_path, tmp_dir, pontos, reencode,
                                       dur, log, on_progress, on_start)

    cortes: list[tuple[float, float]] = []
    for seg in alvos:
        k0 = _keyframe_at_or_after(orig_path, seg.b_start)
        # re-encodado: o keyframe forçado cai no 1º frame >= t (pode ser t +
        # um frame), então o fim também é "at or after"; sem re-encode fica
        # "at or before" (raspa para dentro, nunca come conteúdo dublado)
        k1 = (_keyframe_at_or_after(orig_path, seg.b_end) if reencode
              else _keyframe_at_or_before(orig_path, seg.b_end))
        if reencode and k1 is not None and k1 - seg.b_end > 0.25:
            k1 = _keyframe_at_or_before(orig_path, seg.b_end)
        if k0 is None or k1 is None or k1 - k0 < 0.5:
            log(f"  cut_video {seg.b_start:.1f}-{seg.b_end:.1f}s: sem "
                f"keyframes úteis — fica o preenchimento")
            continue
        cortes.append((k0, k1))
    cortes.sort()
    if not cortes:
        return segs, None, []

    mantidos, pos = [], 0.0
    for c0, c1 in cortes:
        if c0 - pos > 0.05:
            mantidos.append((pos, c0))
        pos = c1
    if dur - pos > 0.05:
        mantidos.append((pos, dur))

    def ts(t):
        h, resto = divmod(max(0.0, t), 3600)
        m, sec = divmod(resto, 60)
        return f"{int(h):02d}:{int(m):02d}:{sec:09.6f}"

    # Cada trecho mantido vira um ARQUIVO do mkvmerge (corte preciso no
    # keyframe, timestamps limpos) e o concat demuxer do ffmpeg os emenda com
    # a duração EXPLÍCITA de cada um. O modo "+" do mkvmerge, que junta
    # direto, sobrepunha ~0,44 s de vídeo E áudio em cada emenda (caso real:
    # 11 cortes = dublagem até 4 s fora do lugar depois do remapeamento).
    spec = ",".join(f"{ts(a)}-{ts(b)}" for a, b in mantidos)
    base = tmp_dir / "orig_parte.mkv"
    p = _run_mkvmerge(["mkvmerge", "-o", str(base),
                       "--split", f"parts:{spec}", orig_path], None)
    partes = sorted(tmp_dir.glob("orig_parte-*.mkv")) or (
        [base] if base.exists() else [])
    if p.returncode >= 2 or len(partes) != len(mantidos):
        log("⚠️ corte do vídeo falhou (mkvmerge) — trechos ficam com áudio "
            "original: " + (p.stdout or p.stderr)[-200:])
        return segs, None, []
    lista = tmp_dir / "orig_partes.txt"
    # (caminhos do tmp_dir: sem aspas para escapar)
    lista.write_text("".join(f"file '{pt}'\nduration {b - a:.6f}\n"
                             for pt, (a, b) in zip(partes, mantidos)),
                     encoding="utf-8")
    cortado = tmp_dir / "orig_cortado.mkv"
    pc = subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error",
                         "-y", "-f", "concat", "-safe", "0", "-i", str(lista),
                         "-map", "0", "-c", "copy", str(cortado)],
                        capture_output=True, text=True, encoding="utf-8",
                        errors="replace")
    for pt in partes:
        pt.unlink(missing_ok=True)
    if pc.returncode != 0 or not cortado.exists():
        log("⚠️ emenda dos trechos falhou (ffmpeg concat) — trechos ficam com "
            "áudio original: " + pc.stderr[-200:])
        return segs, None, []
    total = sum(c1 - c0 for c0, c1 in cortes)
    log(f"cut_video: {len(cortes)} trecho(s), {total:.1f}s removidos do vídeo "
        f"(cortes em keyframe)")

    def mapa(t):
        if t is None:
            return None
        removido = 0.0
        for c0, c1 in cortes:
            if t <= c0:
                break
            removido += min(t, c1) - c0
        return t - removido

    ids_alvo = {id(x) for x in alvos}
    for seg in segs:
        seg.b_start = mapa(seg.b_start)
        seg.b_end = mapa(seg.b_end)
        # o offset é b - a: com b remapeado ele muda junto. Esquecer isto
        # posicionava a fatia dublada (src = b - offset) minutos antes do
        # lugar — e negativa no começo do arquivo, que é o atrim vazio que
        # derrubava o passo 1 com "Invalid data" (caso real: 11 cortes)
        if seg.offset is not None and seg.b_start is not None:
            seg.offset = seg.b_start - seg.a_start
        if id(seg) in ids_alvo:
            # o que sobrou são as raspas das bordas: preenchidas com o
            # original como qualquer gap_orig; a dublagem do trecho (se era
            # replaced, dublava OUTRA cena) é descartada
            seg.kind = "gap_orig"
            seg.a_end = seg.a_start
            seg.extra.pop("action", None)
            seg.note = "cena cortada do vídeo (cut_video)"
    return segs, str(cortado), cortes


def render(segs: list[Segment], dub_path: str, orig_path: str, output: str,
           target_lang: str, log=print, on_progress=None, on_start=None,
           b_window: tuple[float, float] | list | None = None,
           external_subs: dict | None = None,
           original_lang: str | None = None,
           fill_with_original: bool = True,
           video_reencode: dict | None = None):
    """Renderiza a EDL num MKV final. Bloqueante (roda ffmpeg).

    external_subs: {"orig": [paths], "dub": [paths], "orig_lang", "dub_lang"}
    — legendas externas dos torrents, remapeadas AQUI (com a janela e os
    cortes reais) e incluídas no PRÓPRIO mux final: anexar depois obrigava a
    reescrever o arquivo inteiro uma segunda vez (num REMUX, dezenas de GB).

    b_window: (início, fim) em segundos ABSOLUTOS do original quando ele é um
    arquivo FUNDIDO (dois episódios) — o vídeo é cortado nessa janela. O corte
    começa no KEYFRAME anterior ao início (stream copy não corta no meio de
    um GOP) e todos os tempos b são deslocados por esse mesmo valor, então o
    sync não muda; sobra no máximo um GOP do episódio vizinho no começo,
    preenchido com áudio original como qualquer trecho sem dublagem.
    """
    tmp_dir = Path(tempfile.mkdtemp(prefix="edl_render_"))
    # cut_video ANTES de tudo: o original pode ser trocado pela versão cortada
    # (cenas sem dublagem removidas), e o resto do render nem fica sabendo —
    # só trabalha com um arquivo mais curto e uma EDL já remapeada
    if b_window and any(sg.extra.get("action") == "cut_video" for sg in segs):
        log("⚠️ cut_video + janela de fundido no mesmo episódio não é "
            "suportado — os trechos ficam com áudio original")
        _cortado, b_cuts = None, []
    else:
        segs, _cortado, b_cuts = _apply_video_cuts(
            segs, orig_path, tmp_dir, log, reencode=video_reencode,
            on_progress=on_progress, on_start=on_start)
    if _cortado:
        orig_path = _cortado
        # a timeline agora é a do CORTADO: sem isto o planejador ganhava a
        # duração do original inteiro e emitia um preenchimento final além do
        # EOF (atrim vazio → o concat do passo 1 morria com "Invalid data")
        duration_b = float(merger.ffprobe_json(orig_path)["format"]["duration"])

    probe_orig = merger.ffprobe_json(orig_path)
    probe_dub = merger.ffprobe_json(dub_path)
    merger.annotate_type_indexes(probe_orig)
    merger.annotate_type_indexes(probe_dub)
    duration_b = float(probe_orig["format"]["duration"])

    in_opts: list[str] = []
    out_opts: list[str] = []
    if b_window:
        w0, w1 = float(b_window[0]), float(b_window[1])
        kf = _keyframe_at_or_before(orig_path, w0)
        if kf is None:
            kf = w0
            log(f"⚠️ não achei keyframe antes de {w0:.1f}s — cortando em {w0:.1f}s "
                f"(o vídeo pode começar alguns frames adiantado)")
        # -ss de ENTRADA no keyframe (corte limpo em stream copy) e a duração
        # como -t de SAÍDA. NÃO usar -to de entrada: o ffmpeg 7.1 ignorou
        # (E01 fundido saiu com o arquivo inteiro, 81 min) e o 9.0 interpreta
        # relativo ao -ss — semântica instável entre versões; -t na saída é
        # determinístico
        in_opts = ["-ss", f"{kf:.3f}"]
        out_opts = ["-t", f"{w1 - kf:.3f}"]
        # o input passa a começar em 0 = kf: desloca os tempos b da EDL
        segs = [Segment(s.kind, s.a_start, s.a_end,
                        None if s.b_start is None else s.b_start - kf,
                        None if s.b_end is None else s.b_end - kf,
                        s.slope, s.residual, s.confidence,
                        None if s.offset is None else s.offset - kf,
                        s.note, dict(s.extra)) for s in segs]
        duration_b = w1 - kf
        log(f"Original fundido: cortando {kf:.1f}s–{w1:.1f}s "
            f"(keyframe {w0 - kf:.1f}s antes do início do episódio)")

    # faixa dublada: melhor áudio do idioma alvo no arquivo dublado
    iso = merger.canonical_lang(target_lang)
    best = merger.choose_best_audio_per_language([probe_dub], {0: iso})
    dub_stream = (best.get(iso) or best.get("und") or
                  next(iter(best.values()), None))
    if dub_stream is None:
        raise merger.MergeError(f"nenhum áudio no arquivo dublado ({dub_path})")
    dub_a = dub_stream[1]["_type_index"]
    channels = int(dub_stream[1].get("channels") or 2)
    layout = _layout(channels)
    # áudio original para os preenchimentos: o idioma ORIGINAL DA OBRA, nunca
    # "o primeiro que não é o alvo" — num release com faixas rus,rus,eng isso
    # dava RUSSO em todo preenchimento (caso real de campo). Sem saber o
    # idioma original, ou sem faixa dele, o preenchimento vira SILÊNCIO: uma
    # língua aleatória no meio do episódio é pior que um trecho mudo.
    orig_best = merger.choose_best_audio_per_language([probe_orig], {})
    orig_pick = None
    if original_lang:
        want = merger.canonical_lang(
            merger.LANG_ISO.get(original_lang, original_lang))
        if want != iso:
            orig_pick = orig_best.get(want)
        if orig_pick is None:
            log(f"⚠️ o original não tem faixa em {want} — preenchimentos ficam "
                f"em silêncio")
    elif len(orig_best) == 1:
        orig_pick = next(iter(orig_best.values()))
    else:
        log("⚠️ idioma original não informado e o arquivo tem várias faixas — "
            "preenchimentos ficam em silêncio")
    orig_a = orig_pick[1]["_type_index"] if orig_pick else None

    slices, fills = _plan_slices(segs, duration_b)
    # "sem dublagem, sem áudio": o usuário pode preferir o trecho MUDO a
    # ouvir o idioma original no meio do episódio
    if not fill_with_original:
        orig_a = None
    if orig_a is None:
        for sl in slices:
            if sl["src"] == "orig":
                sl["src"] = "silence"
    if not slices:
        raise merger.MergeError("EDL sem nenhum trecho renderizável")

    chains, labels = [], []
    norm = f"aresample=48000,aformat=sample_rates=48000:channel_layouts={layout}"
    for i, sl in enumerate(slices):
        lbl = f"c{i}"
        dur = sl["b_end"] - sl["b_start"]
        if sl["src"] == "silence":
            chains.append(
                f"anullsrc=r=48000:cl={layout},atrim=end={dur:.3f},"
                f"asetpts=PTS-STARTPTS[{lbl}]")
        else:
            inp = f"[1:a:{dub_a}]" if sl["src"] == "dub" else f"[0:a:{orig_a}]"
            start, end = sl["src_start"], sl["src_end"]
            steps = [f"atrim=start={max(0.0, start):.3f}:end={max(0.0, end):.3f}",
                     "asetpts=PTS-STARTPTS", norm]
            if sl.get("tempo") and abs(sl["tempo"] - 1.0) > 0.001:
                # tempo = duração_saída/duração_entrada (PAL: ~1.0427 — o
                # dublado acelerado precisa ESTICAR). rubberband usa fator de
                # VELOCIDADE (inverso) e corrige o pitch junto.
                t = sl["tempo"]
                if has_rubberband():
                    steps.append(f"rubberband=tempo={1 / t:.5f}:pitch={1 / t:.5f}")
                else:
                    # asetrate desacelera duração E pitch juntos (o speedup
                    # PAL alterou os dois juntos) — qualidade um pouco pior
                    steps.append(f"asetrate={int(48000 / t)},aresample=48000")
            # apad+atrim: cada fatia sai EXATAMENTE do tamanho do buraco que
            # preenche — o concat não pode escorregar
            steps += ["apad", f"atrim=end={dur:.3f}", "asetpts=PTS-STARTPTS"]
            chains.append(f"{inp}{','.join(steps)}[{lbl}]")
        labels.append(f"[{lbl}]")
    chains.append(f"{''.join(labels)}concat=n={len(labels)}:v=0:a=1[dub_out]")
    filter_complex = ";".join(chains)

    Path(output).parent.mkdir(parents=True, exist_ok=True)
    codec, bitrate = merger.filtered_codec_and_bitrate(channels)
    log(f"Renderizando EDL: {len(slices)} fatia(s), {len(fills)} preenchimento(s)"
        + ("" if has_rubberband() else " (sem rubberband: fallback asetrate)"))

    # DOIS PASSOS de propósito. Num único ffmpeg, o áudio original entrava no
    # filter_complex (preenchimentos) E saía em stream copy — o mesmo stream
    # decodificado para 20+ buffersrc e copiado para o muxer engasga as filas
    # do ffmpeg (deadlock com 0% para sempre; caso real de campo (S01E06), os
    # episódios anteriores só passaram por sorte de escalonamento). Separar
    # o áudio dublado remontado num .mka e depois só MUXAR cópias elimina a
    # condição por construção.
    dub_mka = tmp_dir / "dub.mka"

    # legendas externas: remapeadas AGORA (janela e cortes já são conhecidos)
    # e incluídas no mux final — anexar depois reescreveria o arquivo inteiro
    subs_prontas: list[dict] = []
    if external_subs and (external_subs.get("orig") or external_subs.get("dub")):
        from services.series import subs as ext_subs
        kf_shift = float(in_opts[1]) if in_opts else 0.0
        orig_fn = (ext_subs.cuts_fn(b_cuts) if b_cuts
                   else ext_subs.shift_fn(-kf_shift))
        # o lado dublado segue a EDL — os segmentos daqui já estão com os
        # cortes aplicados (b remapeado), então a composição sai de graça
        seg_dicts = [{"kind": sg.kind, "a_start": sg.a_start, "a_end": sg.a_end,
                      "b_start": sg.b_start, "b_end": sg.b_end,
                      "offset": sg.offset,
                      "action": sg.extra.get("action")} for sg in segs]
        dub_fn = ext_subs.edl_fn(seg_dicts, kf_shift)
        try:
            subs_prontas = ext_subs.prepare(
                external_subs.get("orig") or [], external_subs.get("dub") or [],
                orig_fn, dub_fn, ext_subs.embedded_text_keys(probe_orig),
                tmp_dir, external_subs.get("orig_video") or orig_path,
                external_subs.get("dub_video") or dub_path,
                external_subs.get("orig_lang") or "und",
                external_subs.get("dub_lang") or "und", log)
        except Exception as e:  # noqa: BLE001 — legenda é acessório
            log(f"⚠️ legendas externas ignoradas ({e})")
            subs_prontas = []

    try:
        # passo 1: só a faixa dublada remontada (áudio, sem vídeo)
        cmd1 = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-progress", "pipe:1",
                *in_opts, "-i", orig_path, "-i", dub_path,
                "-filter_complex", filter_complex,
                "-map", "[dub_out]", "-c:a", codec, "-b:a", bitrate,
                "-vn", "-sn", "-map_chapters", "-1", *out_opts, str(dub_mka)]
        merger._run_ffmpeg_progress(cmd1, duration_b, on_progress, on_start)

        # passo 2: mux com stream copy de tudo (rápido). Com mkvmerge no
        # PATH e sem janela de corte, é ele que muxa: intercala por blocos
        # lendo os inputs por seek, então faixa esparsa (legenda forçada)
        # nunca segura A/V na memória nem sai do lugar — o modo de falha do
        # muxer do ffmpeg simplesmente não existe nele.
        if has_mkvmerge() and not in_opts:
            cmd2 = _mkvmerge_cmd(str(output), orig_path, str(dub_mka),
                                 iso, probe_orig, subs_prontas)
            p2 = _run_mkvmerge(cmd2, on_progress)
            if p2.returncode >= 2:   # 1 = só avisos; 2 = erro de verdade
                raise merger.MergeError(
                    "mux final (mkvmerge) falhou: "
                    + merger.describe_exit(p2.returncode, p2.stdout or p2.stderr))
            _check_mux_duration(output, duration_b)
            _log_subs(subs_prontas, log)
            return {"b_shift": 0.0, "b_cuts": b_cuts,
                    "subs_muxed": len(subs_prontas)}

        # loglevel WARNING (não error): é em nível de aviso que o ffmpeg diz
        # "forcing output" — o sinal de que a intercalação saiu frouxa. Com
        # -loglevel error esse aviso nunca chegaria ao usuário.
        cmd2 = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
                "-progress", "pipe:1", "-nostats", "-fflags", "+genpts",
                *in_opts, "-i", orig_path, "-i", str(dub_mka)]
        ch_file = None
        if in_opts:
            # janela: capítulos filtrados e deslocados (entrada 2)
            ch_file = _window_chapters(probe_orig, kf, w1, tmp_dir)
            if ch_file is not None:
                cmd2 += ["-f", "ffmetadata", "-i", str(ch_file)]
        sub_in0 = 2 + (1 if ch_file is not None else 0)
        for it in subs_prontas:
            cmd2 += ["-i", it["srt"]]
        cmd2 += ["-map", "0:v:0", "-c:v", "copy"]
        # áudios do original (todas as línguas), depois o dublado
        n_orig_a = len(merger.get_streams(probe_orig, "audio"))
        for k in range(n_orig_a):
            cmd2 += ["-map", f"0:a:{k}"]
        cmd2 += ["-map", "1:a:0", "-c:a", "copy",
                 f"-metadata:s:a:{n_orig_a}", f"language={iso}",
                 f"-disposition:a:{n_orig_a}", "default"]
        # legendas do original intactas — TODAS: legenda comum é o motivo
        # de existirem, e é a intercalação que se adapta a elas (abaixo)
        n_orig_s = len(merger.get_streams(probe_orig, "subtitle"))
        if n_orig_s:
            cmd2 += ["-map", "0:s?", "-c:s", "copy"]
        for k, it in enumerate(subs_prontas):
            idx = n_orig_s + k
            titulo = {"forced": "Forçada", "sdh": "SDH"}.get(it["flavor"], "")
            cmd2 += ["-map", f"{sub_in0 + k}:0", f"-c:s:{idx}", "srt",
                     f"-metadata:s:s:{idx}", f"language={it['lang']}",
                     f"-metadata:s:s:{idx}", f"title={titulo}",
                     f"-disposition:s:{idx}",
                     "forced" if it["flavor"] == "forced" else "0"]
        # capítulos do original, sempre. Com JANELA, não vale copiar do
        # arquivo (-map_chapters 0): o ffmpeg mantém capítulos além do -t, e
        # a duração DECLARADA do MKV vira a do capítulo mais distante — o
        # player mostra 81 min num episódio de 40 (caso real: primeira
        # metade de um fundido). Entram só os da janela, já deslocados.
        if in_opts:
            cmd2 += ["-map_chapters", "2"] if ch_file else ["-map_chapters", "-1"]
        else:
            cmd2 += ["-map_chapters", "0"]
        # intercalação DIMENSIONADA (merger.MUX_BUFFER_GB): num 1080p sai em
        # dezenas de minutos (na prática, estrita — sem ela o muxer "força
        # saída" nas faixas esparsas e players travam); num REMUX 4K limita a
        # memória por construção (estrita de verdade já segurou 31 GB aqui)
        cmd2 += ["-avoid_negative_ts", "make_zero", "-max_interleave_delta",
                 str(merger.sized_interleave_delta(merger.byte_rate_of(probe_orig))),
                 *out_opts, output]
        p2 = _run_mux(cmd2, duration_b, on_progress)
        if p2.returncode != 0:
            raise merger.MergeError(
                "mux final falhou: "
                + merger.describe_exit(p2.returncode, p2.stderr))
        warn = merger.interleave_warning(p2.stderr)
        if warn:
            log(warn)
        # truncamento silencioso: sob pressão de memória o ffmpeg já saiu com
        # código 0 e um arquivo pela METADE — melhor um erro claro aqui do que
        # um episódio faltando o final na estante
        _check_mux_duration(output, duration_b)
    finally:
        dub_mka.unlink(missing_ok=True)
        if _cortado:
            Path(_cortado).unlink(missing_ok=True)
        try:
            tmp_dir.rmdir()
        except OSError:
            pass
    # deslocamento aplicado aos tempos b (0 sem b_window): quem for anexar
    # legendas externas precisa dele
    _log_subs(subs_prontas, log)
    return {"b_shift": float(in_opts[1]) if in_opts else 0.0, "b_cuts": b_cuts,
            "subs_muxed": len(subs_prontas)}


def _ffmeta_escape(text: str) -> str:
    """Valor no formato ffmetadata: '=', ';', '#' e '\\' são especiais."""
    out = text.replace("\\", "\\\\")
    for ch in "=;#":
        out = out.replace(ch, "\\" + ch)
    return out.replace("\n", " ")


def _window_chapters(probe_orig: dict, kf: float, w1: float,
                     tmp_dir: Path) -> Path | None:
    """Arquivo ffmetadata com os capítulos do original que caem na janela
    [kf, w1], deslocados para o tempo do arquivo cortado. None se nenhum
    sobrar (aí o mux vai sem capítulos)."""
    lines = [";FFMETADATA1"]
    total = 0
    for c in probe_orig.get("chapters") or []:
        try:
            start = float(c.get("start_time"))
            end = float(c.get("end_time"))
        except (TypeError, ValueError):
            continue
        if end <= kf or start >= w1:
            continue
        s = max(0.0, start - kf)
        e = min(w1 - kf, end - kf)
        if e - s < 0.5:
            continue     # raspa de capítulo vizinho cortado: não é capítulo
        lines += ["[CHAPTER]", "TIMEBASE=1/1000",
                  f"START={int(round(s * 1000))}", f"END={int(round(e * 1000))}"]
        title = ((c.get("tags") or {}).get("title") or "").strip()
        if title:
            lines.append(f"title={_ffmeta_escape(title)}")
        total += 1
    if not total:
        return None
    f = tmp_dir / "chapters.ffmeta"
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return f


def _log_subs(prontas: list[dict], log) -> None:
    if prontas:
        log("Legendas externas no mux final: " + ", ".join(
            f"{Path(it['path']).name} → {it['lang']}"
            + (f" ({it['flavor']})" if it['flavor'] != 'normal' else "")
            for it in prontas))


_MKV_PROGRESS = re.compile(r"Progress:\s*(\d+(?:\.\d+)?)%")


@lru_cache(maxsize=1)
def has_mkvmerge() -> bool:
    import shutil
    return shutil.which("mkvmerge") is not None


def _mkvmerge_cmd(output: str, orig_path: str, dub_mka: str, iso: str,
                  probe_orig: dict,
                  extra_subs: list[dict] | None = None) -> list[str]:
    """Comando do mkvmerge para o mux final: tudo do original (vídeo, áudios,
    legendas, capítulos, anexos) + a faixa dublada remontada, que entra com o
    idioma alvo e como faixa padrão (os áudios do original perdem o padrão).

    Os ids de faixa do mkvmerge seguem a ordem das faixas no arquivo — a mesma
    do ffprobe, então o `index` de cada stream serve de id."""
    cmd = ["mkvmerge", "-o", output]
    for st in merger.get_streams(probe_orig, "audio"):
        cmd += ["--default-track-flag", f"{int(st['index'])}:no"]
    cmd += [orig_path,
            "--language", f"0:{iso}",
            "--default-track-flag", "0:yes",
            dub_mka]
    for it in extra_subs or []:
        titulo = {"forced": "Forçada", "sdh": "SDH"}.get(it["flavor"], "")
        cmd += ["--language", f"0:{it['lang']}",
                "--track-name", f"0:{titulo}",
                "--default-track-flag", "0:no",
                "--forced-display-flag",
                "0:" + ("yes" if it["flavor"] == "forced" else "no"),
                it["srt"]]
    return cmd


def _check_mux_duration(output: str, expected_s: float) -> None:
    """A duração da saída bate com a esperada? (código 0 não basta: ver acima)"""
    try:
        got = float(merger.ffprobe_json(str(output))["format"]["duration"])
    except (merger.MergeError, KeyError, TypeError, ValueError):
        raise merger.MergeError("mux final saiu ilegível (sem duração)")
    if got < expected_s - 30.0:
        raise merger.MergeError(
            f"mux final saiu TRUNCADO: {got:.0f}s de {expected_s:.0f}s "
            f"esperados — quase sempre falta de memória no muxer")


def _run_mkvmerge(cmd: list[str], on_progress=None) -> subprocess.CompletedProcess:
    """mkvmerge com progresso: ele imprime 'Progress: NN%' na saída padrão
    enquanto copia. Sem isso o mux de um REMUX de 150 GB fica uma hora sem
    dar sinal e parece travado."""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace",
                            bufsize=1, preexec_fn=merger.mem_limiter())
    erros: list[str] = []
    dreno = threading.Thread(target=lambda: erros.extend(proc.stderr), daemon=True)
    dreno.start()
    linhas: list[str] = []
    for line in proc.stdout:
        linhas.append(line)
        m = _MKV_PROGRESS.search(line)
        if m and on_progress:
            on_progress({"pct": float(m.group(1)), "out_s": 0.0,
                         "duration_s": 0.0, "size": 0, "bitrate": 0,
                         "speed": 0.0, "fps": 0.0, "eta": None})
    proc.stdout.close()
    proc.wait()
    dreno.join(timeout=5)
    proc.stderr.close()
    if on_progress and proc.returncode < 2:
        on_progress({"pct": 100.0, "out_s": 0.0, "duration_s": 0.0, "size": 0,
                     "bitrate": 0, "speed": 0.0, "fps": 0.0, "eta": 0})
    return subprocess.CompletedProcess(cmd, proc.returncode,
                                       "".join(linhas), "".join(erros))


def _run_mux(cmd: list[str], duration_s: float = 0.0,
             on_progress=None) -> subprocess.CompletedProcess:
    """Mux final (cópia pura) com o teto de memória do merger: um muxer que
    dispara é problema DELE, não da máquina."""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace",
                            bufsize=1, preexec_fn=merger.mem_limiter())
    # stderr numa thread: lendo os dois em sequência, um mux que avisa muito
    # (uma linha "forcing output" por faixa esparsa) encheria o pipe e travaria
    erros: list[str] = []
    dreno = threading.Thread(target=lambda: erros.extend(proc.stderr), daemon=True)
    dreno.start()
    bloco: dict = {}
    for line in proc.stdout:
        key, _, value = line.strip().partition("=")
        if key != "progress":
            bloco[key] = value
            continue
        if on_progress and duration_s > 0:
            info = merger._parse_progress_block(bloco, duration_s)
            if value == "end":
                info.update(pct=100.0, eta=0)
            on_progress(info)
        bloco = {}
    proc.stdout.close()
    proc.wait()
    dreno.join(timeout=5)
    proc.stderr.close()
    return subprocess.CompletedProcess(cmd, proc.returncode, "", "".join(erros))


def _plan_slices(segs: list[Segment],
                 duration_b: float) -> tuple[list[dict], list[dict]]:
    """EDL -> fatias contíguas na timeline do ORIGINAL (b), sem buracos.

    Retorna (fatias, preenchimentos). Regiões do original fora de qualquer
    segmento (pontas, arredondamento) também são preenchidas com o áudio
    original — o concat precisa cobrir 0..duração sem escorregar."""
    slices: list[dict] = []
    fills: list[dict] = []
    cursor = 0.0

    def fill(b0: float, b1: float, src: str, why: str):
        if b1 - b0 <= _EPS:
            return
        slices.append({"src": "silence" if src == "silence" else "orig",
                       "src_start": b0, "src_end": b1,
                       "b_start": b0, "b_end": b1})
        fills.append({"b_start": b0, "b_end": b1, "why": why})

    ordered = sorted((s for s in segs if s.b_start is not None
                      and (s.b_end or 0) > (s.b_start or 0)),
                     key=lambda s: s.b_start)
    for seg in ordered:
        b0, b1 = max(0.0, seg.b_start), min(duration_b, seg.b_end)
        if b1 <= cursor + _EPS:
            continue
        b0 = max(b0, cursor)
        if b0 > cursor + _EPS:
            hole = b0 - cursor
            if hole <= SMALL_HOLE_S and slices and slices[-1]["src"] == "dub" \
                    and not slices[-1].get("tempo"):
                # sobra de arredondamento entre duas fatias de dublagem
                # (fronteiras do vídeo vêm quantizadas a 0,25 s): a dublagem
                # anterior CONTINUA por esses ms — jamais áudio original no
                # meio da fala
                slices[-1]["src_end"] += hole
                slices[-1]["b_end"] += hole
            else:
                # buraco de verdade não descrito pela EDL: preenche com o original
                fill(cursor, b0, "orig", "trecho sem correspondência mapeada")
        action = seg.extra.get("action")
        if seg.kind in ("match", "drift"):
            slices.append({"src": "dub",
                           "src_start": b0 - seg.offset,
                           "src_end": b1 - seg.offset,
                           "b_start": b0, "b_end": b1})
        elif seg.kind == "pal":
            a_dur = seg.a_end - seg.a_start
            b_dur = (seg.b_end - seg.b_start) or a_dur
            slices.append({"src": "dub", "src_start": seg.a_start,
                           "src_end": seg.a_end, "b_start": b0, "b_end": b1,
                           "tempo": b_dur / a_dur if a_dur else 1.0})
        elif seg.kind == "gap_orig":
            fill(b0, b1, "silence" if action == "silence" else "orig",
                 "cena sem dublagem")
        elif seg.kind == "replaced":
            if action == "use_dub":
                slices.append({"src": "dub", "src_start": seg.a_start,
                               "src_end": seg.a_end, "b_start": b0, "b_end": b1})
            else:
                fill(b0, b1, "silence" if action == "silence" else "orig",
                     "cena substituída")
        cursor = b1
    if cursor < duration_b - _EPS:
        fill(cursor, duration_b, "orig", "final sem correspondência mapeada")
    return slices, fills
