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

Cada preenchimento vira um CAPÍTULO no MKV ("[preenchido] 12:34-12:52") —
auditável depois, sem caçar de ouvido.
"""
import subprocess
import tempfile
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


def render(segs: list[Segment], dub_path: str, orig_path: str, output: str,
           target_lang: str, log=print, on_progress=None, on_start=None,
           b_window: tuple[float, float] | list | None = None):
    """Renderiza a EDL num MKV final. Bloqueante (roda ffmpeg).

    b_window: (início, fim) em segundos ABSOLUTOS do original quando ele é um
    arquivo FUNDIDO (dois episódios) — o vídeo é cortado nessa janela. O corte
    começa no KEYFRAME anterior ao início (stream copy não corta no meio de
    um GOP) e todos os tempos b são deslocados por esse mesmo valor, então o
    sync não muda; sobra no máximo um GOP do episódio vizinho no começo,
    preenchido com áudio original como qualquer trecho sem dublagem.
    """
    probe_orig = merger.ffprobe_json(orig_path)
    probe_dub = merger.ffprobe_json(dub_path)
    merger.annotate_type_indexes(probe_orig)
    merger.annotate_type_indexes(probe_dub)
    duration_b = float(probe_orig["format"]["duration"])

    in_opts: list[str] = []
    if b_window:
        w0, w1 = float(b_window[0]), float(b_window[1])
        kf = _keyframe_at_or_before(orig_path, w0)
        if kf is None:
            kf = w0
            log(f"⚠️ não achei keyframe antes de {w0:.1f}s — cortando em {w0:.1f}s "
                f"(o vídeo pode começar alguns frames adiantado)")
        in_opts = ["-ss", f"{kf:.3f}", "-to", f"{w1:.3f}"]
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
    # áudio original para os preenchimentos: o melhor de qualquer língua ≠ alvo
    orig_best = merger.choose_best_audio_per_language([probe_orig], {})
    orig_pick = next((v for k, v in orig_best.items() if k != iso),
                     next(iter(orig_best.values()), None))
    if orig_pick is None:
        raise merger.MergeError(f"nenhum áudio no arquivo original ({orig_path})")
    orig_a = orig_pick[1]["_type_index"]

    slices, fills = _plan_slices(segs, duration_b)
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
    # do ffmpeg (deadlock com 0% para sempre; caso real Mr Robot S01E06, os
    # episódios anteriores só passaram por sorte de escalonamento). Separar
    # o áudio dublado remontado num .mka e depois só MUXAR cópias elimina a
    # condição por construção.
    tmp_dir = Path(tempfile.mkdtemp(prefix="edl_render_"))
    dub_mka = tmp_dir / "dub.mka"
    chapters_file = None
    try:
        # passo 1: só a faixa dublada remontada (áudio, sem vídeo)
        cmd1 = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-progress", "pipe:1",
                *in_opts, "-i", orig_path, "-i", dub_path,
                "-filter_complex", filter_complex,
                "-map", "[dub_out]", "-c:a", codec, "-b:a", bitrate,
                "-vn", "-sn", str(dub_mka)]
        merger._run_ffmpeg_progress(cmd1, duration_b, on_progress, on_start)

        # passo 2: mux com stream copy de tudo (rápido)
        cmd2 = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-fflags", "+genpts",
                *in_opts, "-i", orig_path, "-i", str(dub_mka)]
        if fills:
            chapters_file = _chapters_metadata(fills)
            cmd2 += ["-f", "ffmetadata", "-i", chapters_file]
        cmd2 += ["-map", "0:v:0", "-c:v", "copy"]
        # áudios do original (todas as línguas), depois o dublado
        n_orig_a = len(merger.get_streams(probe_orig, "audio"))
        for k in range(n_orig_a):
            cmd2 += ["-map", f"0:a:{k}"]
        cmd2 += ["-map", "1:a:0", "-c:a", "copy",
                 f"-metadata:s:a:{n_orig_a}", f"language={iso}",
                 f"-disposition:a:{n_orig_a}", "default"]
        # legendas do original intactas
        if merger.get_streams(probe_orig, "subtitle"):
            cmd2 += ["-map", "0:s?", "-c:s", "copy"]
        # capítulos: com preenchimentos, os capítulos AUDITÁVEIS dos fills
        # entram no lugar dos originais; num corte de arquivo fundido os do
        # original não valem (tempos do episódio duplo)
        cmd2 += ["-map_chapters", "2" if fills else ("-1" if b_window else "0")]
        # -max_interleave_delta 0: intercalação estrita do MKV — sem isso o
        # muxer "força saída" quando há stream esparsa (legenda) e alguns
        # players travam a reprodução (caso real anterior). É seguro aqui
        # porque este passo é SÓ cópia (nenhum filtergraph disputando fila).
        cmd2 += ["-avoid_negative_ts", "make_zero", "-max_interleave_delta", "0",
                 output]
        p2 = subprocess.run(cmd2, capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
        if p2.returncode != 0:
            raise merger.MergeError(
                f"mux final falhou: {p2.stderr.strip()[-800:]}")
    finally:
        if chapters_file:
            Path(chapters_file).unlink(missing_ok=True)
        dub_mka.unlink(missing_ok=True)
        try:
            tmp_dir.rmdir()
        except OSError:
            pass


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


def _chapters_metadata(fills: list[dict]) -> str:
    """Arquivo ffmetadata com um capítulo por preenchimento (auditoria)."""
    lines = [";FFMETADATA1"]
    for f in fills:
        lines += ["[CHAPTER]", "TIMEBASE=1/1000",
                  f"START={int(f['b_start'] * 1000)}",
                  f"END={int(f['b_end'] * 1000)}",
                  f"title=[preenchido] {f['why']}"]
    fd = tempfile.NamedTemporaryFile("w", suffix=".ffmeta", delete=False,
                                     encoding="utf-8")
    fd.write("\n".join(lines) + "\n")
    fd.close()
    return fd.name
