"""Merge por SEGMENTOS — versão incrementada do merger.merge, sem alterá-lo.

Para cortes/versões diferentes (o caso em que o merge clássico avisa "drift"),
um offset constante não basta: o desalinhamento MUDA ao longo do filme. Aqui o
filme é fatiado nos cortes de cena e cada fatia é alinhada separadamente:

1. Detecta cortes no arquivo de REFERÊNCIA (o de melhor vídeo) com os filtros
   do ffmpeg: blackdetect (trechos pretos no vídeo) e silencedetect (silêncio
   no áudio).
2. Validação CRUZADA: um corte só vale se um trecho preto coincidir com um
   trecho de silêncio (tolerância configurável) — preto com música por cima é
   transição estilística, silêncio com imagem é só cena parada; os dois juntos
   é corte de verdade. Dá para usar um detector só (use_black/use_silence).
3. O filme vira N segmentos e o offset (GCC-PHAT) é medido POR SEGMENTO.
   Segmento curto demais para medir herda o offset do vizinho.
4. Se todos os offsets concordam (≤ OFFSET_AGREEMENT), delega ao merge
   CLÁSSICO — offset constante com stream copy é estritamente melhor do que
   re-encodar. Se divergem, o áudio dublado é remontado fatia a fatia
   (asplit + atrim + concat) e re-encodado (AAC estéreo / AC3 multicanal).

Todos os thresholds têm default e são ajustáveis via SegmentParams (no CLI:
--black-*, --silence-*, --match-tolerance, --min-segment...).
"""
from __future__ import annotations

import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from services import merger
from services.merger import MergeError, MergeResult

# offsets de segmentos que diferem menos que isto são "o mesmo offset"
# (mesmo limiar da validação em duas janelas do merge clássico)
OFFSET_AGREEMENT = 0.15   # s
MIN_MEASURE_WINDOW = 8.0  # s de áudio no mínimo para o GCC-PHAT ser confiável


@dataclass
class SegmentParams:
    """Parâmetros da detecção/segmentação — defaults pensados para filmes.

    blackdetect: black_min_dur (d=) é a duração mínima do trecho preto;
    black_pix_th (pix_th) o quão escuro um pixel precisa ser (0-1);
    black_pic_th (picture_black_ratio_th) a fração da imagem que precisa
    estar preta.

    silencedetect: silence_noise_db (noise=, em dB — mais negativo = exige
    silêncio mais absoluto) e silence_min_dur (d=).

    match_tolerance: distância máxima (s) entre o trecho preto e o de
    silêncio para a validação cruzada aceitar o corte.

    min_segment: segmento menor que isto não nasce (cortes muito próximos
    são descartados) — precisa haver áudio suficiente para medir offset.

    seg_align_dur: teto (s) da janela de correlação dentro de cada segmento.
    """
    black_min_dur: float = 0.4
    black_pix_th: float = 0.10
    black_pic_th: float = 0.98
    silence_noise_db: float = -50.0
    silence_min_dur: float = 0.3
    match_tolerance: float = 0.5
    min_segment: float = 30.0
    seg_align_dur: float = 120.0
    use_black: bool = True
    use_silence: bool = True


# -------------------- detecção (blackdetect / silencedetect) --------------------

_BLACK_RE = re.compile(r"black_start:([\d.]+)\s+black_end:([\d.]+)")
_SIL_START_RE = re.compile(r"silence_start:\s*(-?[\d.]+)")
_SIL_END_RE = re.compile(r"silence_end:\s*([\d.]+)")


def _ffmpeg_stderr(args: list[str]) -> str:
    """Roda o ffmpeg jogando a saída fora (-f null) e devolve o stderr,
    onde blackdetect/silencedetect imprimem os intervalos."""
    p = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", *args, "-f", "null", "-"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if p.returncode != 0:
        raise MergeError(f"ffmpeg (detecção) falhou: {p.stderr.strip()[-800:]}")
    return p.stderr


def parse_blackdetect(stderr: str) -> list[tuple[float, float]]:
    return [(float(a), float(b)) for a, b in _BLACK_RE.findall(stderr)]


def parse_silencedetect(stderr: str, duration: float) -> list[tuple[float, float]]:
    """Pareia silence_start/silence_end; um start sem end (silêncio até o fim
    do arquivo) fecha na duração total."""
    out = []
    start = None
    for line in stderr.splitlines():
        m = _SIL_START_RE.search(line)
        if m:
            start = max(0.0, float(m.group(1)))
            continue
        m = _SIL_END_RE.search(line)
        if m and start is not None:
            out.append((start, float(m.group(1))))
            start = None
    if start is not None:
        out.append((start, duration))
    return out


def detect_black(path: str, v_idx: int, p: SegmentParams) -> list[tuple[float, float]]:
    stderr = _ffmpeg_stderr([
        "-i", path, "-map", f"0:v:{v_idx}", "-an", "-sn",
        "-vf", (f"blackdetect=d={p.black_min_dur}:pix_th={p.black_pix_th}"
                f":picture_black_ratio_th={p.black_pic_th}")])
    return parse_blackdetect(stderr)


def detect_silence(path: str, a_idx: int, duration: float,
                   p: SegmentParams) -> list[tuple[float, float]]:
    stderr = _ffmpeg_stderr([
        "-i", path, "-map", f"0:a:{a_idx}", "-vn", "-sn",
        "-af", f"silencedetect=noise={p.silence_noise_db}dB:d={p.silence_min_dur}"])
    return parse_silencedetect(stderr, duration)


# -------------------- cortes -> segmentos --------------------

def cross_validate(blacks: list[tuple[float, float]],
                   silences: list[tuple[float, float]],
                   tolerance: float) -> list[float]:
    """Cortes confirmados: trecho preto que coincide (± tolerância) com um
    trecho de silêncio. O ponto de corte é o meio do trecho preto."""
    cuts = []
    for bs, be in blacks:
        for ss, se in silences:
            if bs <= se + tolerance and be >= ss - tolerance:
                cuts.append((bs + be) / 2)
                break
    return cuts


def filter_cuts(cuts: list[float], duration: float, min_segment: float) -> list[float]:
    """Descarta cortes que criariam segmentos menores que min_segment
    (do início, do fim ou do corte anterior)."""
    out = []
    last = 0.0
    for c in sorted(cuts):
        if c - last >= min_segment and duration - c >= min_segment:
            out.append(c)
            last = c
    return out


def build_segments(cuts: list[float], duration: float) -> list[tuple[float, float]]:
    bounds = [0.0, *cuts, duration]
    return [(bounds[i], bounds[i + 1]) for i in range(len(bounds) - 1)]


# -------------------- offset por segmento --------------------

def _extract_wav_window(path: str, a_idx: int, out_wav: str, start: float, dur: float):
    """Como merger._extract_mono_wav, mas com duração configurável (a janela
    precisa caber dentro do segmento)."""
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           "-ss", f"{start:.3f}", "-i", path,
           "-map", f"0:a:{a_idx}", "-vn", "-sn",
           "-ac", "1", "-ar", str(merger.ALIGN_SR), "-acodec", "pcm_s16le",
           "-t", f"{dur:.3f}", out_wav]
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if p.returncode != 0:
        raise MergeError(f"falha ao extrair áudio de '{path}': {p.stderr.strip()}")


def _measure_window(ref_path: str, ref_a: int, oth_path: str, oth_a: int,
                    start: float, dur: float) -> float:
    with tempfile.TemporaryDirectory(prefix="seg_align_") as td:
        wref, woth = str(Path(td) / "ref.wav"), str(Path(td) / "oth.wav")
        _extract_wav_window(ref_path, ref_a, wref, start, dur)
        _extract_wav_window(oth_path, oth_a, woth, start, dur)
        return merger.gcc_phat_delay_seconds(
            merger._read_wav(woth), merger._read_wav(wref), merger.ALIGN_SR)


def measure_segment_offsets(ref_path: str, ref_a: int, oth_path: str, oth_a: int,
                            segments: list[tuple[float, float]],
                            p: SegmentParams, log=print,
                            oth_duration: float | None = None) -> list[float]:
    """Offset (s) de cada segmento; imensurável herda o vizinho.

    Imensurável = curto demais, além do fim do arquivo dublado (os segmentos
    vêm da linha do tempo da REFERÊNCIA — se o dublado é mais curta, os
    últimos segmentos não existem nele e a extração viria vazia), ou extração
    que falhou por qualquer outro motivo (áudio mais curto que o container,
    fim corrompido...). Nada disso derruba o merge: só aquele segmento fica
    sem medição própria.
    """
    offsets: list[float | None] = []
    for s, e in segments:
        seg = e - s
        margin = min(3.0, seg * 0.1)  # afasta do corte (fade/transição)
        dur = min(seg - 2 * margin, p.seg_align_dur)
        if dur < MIN_MEASURE_WINDOW:
            offsets.append(None)
            log(f"  segmento {s:9.2f}-{e:9.2f}s: curto demais para medir — herda o vizinho")
            continue
        start = s + max(margin, (seg - dur) / 2)  # janela centrada no segmento
        if oth_duration:
            # a extração dos dois arquivos usa o MESMO start absoluto: janela
            # que não cabe no dublado sairia vazia/curta demais para o GCC-PHAT
            if start + MIN_MEASURE_WINDOW > oth_duration:
                offsets.append(None)
                log(f"  segmento {s:9.2f}-{e:9.2f}s: além do fim do arquivo dublado "
                    f"({oth_duration:.0f}s) — herda o vizinho")
                continue
            dur = min(dur, oth_duration - start)
        try:
            tau = _measure_window(ref_path, ref_a, oth_path, oth_a, start, dur)
        except MergeError as err:
            offsets.append(None)
            log(f"  ⚠️ segmento {s:9.2f}-{e:9.2f}s: medição falhou ({err}) — herda o vizinho")
            continue
        offsets.append(tau)
        log(f"  segmento {s:9.2f}-{e:9.2f}s: offset {tau * 1000:+8.1f} ms")

    measured = [o for o in offsets if o is not None]
    if not measured:
        raise MergeError("nenhum segmento longo o bastante para medir o offset")
    # herda: None pega o último medido antes dele (os do início pegam o primeiro)
    prev = measured[0]
    for i, o in enumerate(offsets):
        if o is None:
            offsets[i] = prev
        else:
            prev = o
    return offsets  # type: ignore[return-value]


# -------------------- montagem do áudio segmentado --------------------

def build_segment_chain(input_spec: str, segments: list[tuple[float, float]],
                        offsets: list[float], out_label: str) -> str:
    """Filtro que remonta um áudio do arquivo dublado fatia a fatia.

    Para o segmento [s,e) da referência com offset τ, o trecho correspondente
    do dublado é [s+τ, e+τ). Cada fatia é cortada (atrim), re-zerada
    (asetpts), completada com silêncio se faltar cauda (apad+atrim garante a
    duração EXATA do segmento — o concat não pode escorregar) e, se o começo
    cair antes do 0 do arquivo, ganha silêncio na frente (adelay).
    """
    n = len(segments)
    split_labels = "".join(f"[sp_{out_label}_{i}]" for i in range(n))
    chains = [f"{input_spec}asplit={n}{split_labels}"]
    concat_in = []
    for i, ((s, e), tau) in enumerate(zip(segments, offsets)):
        a, b = s + tau, e + tau
        seg_dur = e - s
        steps = [f"atrim=start={max(0.0, a):.6f}:end={max(0.0, b):.6f}",
                 "asetpts=PTS-STARTPTS"]
        if a < 0:  # começo antes do arquivo: silêncio na frente
            steps.append(f"adelay={int(round(-a * 1000))}:all=1")
        steps += ["apad", f"atrim=end={seg_dur:.6f}", "asetpts=PTS-STARTPTS"]
        chains.append(f"[sp_{out_label}_{i}]" + ",".join(steps) + f"[c_{out_label}_{i}]")
        concat_in.append(f"[c_{out_label}_{i}]")
    chains.append("".join(concat_in) + f"concat=n={n}:v=0:a=1[{out_label}]")
    return "; ".join(chains)


# -------------------- merge segmentado --------------------

def merge_segmented(file1: str, file2: str, output: str, target_lang: str | None = None,
                    file2_is_target_dub: bool = True, log=print, on_progress=None,
                    params: SegmentParams | None = None) -> MergeResult:
    """Mesma assinatura de uso do merger.merge, com alinhamento POR SEGMENTO.

    Quando os offsets de todos os segmentos concordam, delega ao merge
    clássico (offset constante com stream copy — melhor resultado possível).
    """
    p = params or SegmentParams()
    if not p.use_black and not p.use_silence:
        raise MergeError("pelo menos um detector (black/silence) precisa estar ativo")
    merger._check_tools()
    for f in (file1, file2):
        if not Path(f).exists():
            raise MergeError(f"arquivo não existe: {f}")

    result = MergeResult(output=output)
    target_iso = (merger.canonical_lang(merger.LANG_ISO.get(target_lang, target_lang))
                  if target_lang else None)

    probes = [merger.ffprobe_json(file1), merger.ffprobe_json(file2)]
    for pr in probes:
        merger.annotate_type_indexes(pr)

    ref_input, best_v = merger.choose_best_video(probes)
    other_input = 1 - ref_input
    ref_path = file1 if ref_input == 0 else file2
    oth_path = file2 if ref_input == 0 else file1
    log(f"Melhor vídeo: {ref_path}")

    und_lang_by_input: dict[int, str] = {}
    if target_iso and file2_is_target_dub:
        und_lang_by_input[1] = target_iso

    # atalho do clássico: o melhor vídeo já tem o áudio alvo -> hardlink
    if target_iso:
        ref_langs = {merger._lang_of_stream(s, ref_input, und_lang_by_input)
                     for s in merger.get_streams(probes[ref_input], "audio")}
        if target_iso in ref_langs:
            log(f"O arquivo de melhor vídeo já tem áudio '{target_iso}' — pulando merge, criando hardlink.")
            out_path = Path(output).with_suffix(Path(ref_path).suffix)
            merger._link_or_copy(Path(ref_path), out_path, result.notes)
            result.output = str(out_path)
            result.linked = True
            for n in result.notes:
                log(n)
            return result

    duration = merger._duration_of(probes[ref_input])
    if duration <= 0:
        raise MergeError("não consegui ler a duração do arquivo de referência")
    ref_align_a, oth_align_a = merger.choose_alignment_pair(probes, ref_input)

    # ---- 1. detecção dos cortes no arquivo de referência ----
    blacks = silences = None
    if p.use_black:
        log(f"Detectando trechos pretos (d>={p.black_min_dur}s, pix_th={p.black_pix_th}, "
            f"pic_th={p.black_pic_th})...")
        blacks = detect_black(ref_path, int(best_v["_type_index"]), p)
        log(f"  {len(blacks)} trecho(s) preto(s)")
    if p.use_silence:
        log(f"Detectando silêncios (noise={p.silence_noise_db}dB, d>={p.silence_min_dur}s)...")
        silences = detect_silence(ref_path, ref_align_a, duration, p)
        log(f"  {len(silences)} silêncio(s)")

    if blacks is not None and silences is not None:
        cuts = cross_validate(blacks, silences, p.match_tolerance)
        log(f"Validação cruzada (±{p.match_tolerance}s): {len(cuts)} corte(s) confirmado(s)")
    elif blacks is not None:
        cuts = [(a + b) / 2 for a, b in blacks]
    else:
        cuts = [(a + b) / 2 for a, b in silences]

    cuts = filter_cuts(cuts, duration, p.min_segment)
    segments = build_segments(cuts, duration)
    log(f"{len(segments)} segmento(s) (min_segment={p.min_segment}s)")

    # ---- 2. offset por segmento ----
    log("Medindo o offset de cada segmento (GCC-PHAT)...")
    offsets = measure_segment_offsets(ref_path, ref_align_a, oth_path, oth_align_a,
                                      segments, p, log,
                                      oth_duration=merger._duration_of(probes[other_input]))

    spread = max(offsets) - min(offsets)
    if len(segments) == 1 or spread <= OFFSET_AGREEMENT:
        log(f"Offsets concordam entre os segmentos (variação {spread * 1000:.0f} ms) — "
            f"delegando ao merge clássico (offset constante, stream copy).")
        result = merger.merge(file1, file2, output, target_lang=target_lang,
                              file2_is_target_dub=file2_is_target_dub,
                              log=log, on_progress=on_progress, allow_drift=True)
        result.notes.append(
            f"modo --segments: {len(segments)} segmento(s) medidos, offsets consistentes "
            f"(variação {spread * 1000:.0f} ms) — merge clássico usado")
        return result

    log(f"Offsets DIVERGEM entre segmentos (variação {spread * 1000:.0f} ms) — "
        f"remontando o áudio dublado fatia a fatia.")
    result.notes.append(
        f"{len(segments)} segmentos com offsets de {min(offsets) * 1000:+.0f} a "
        f"{max(offsets) * 1000:+.0f} ms — áudio remontado por segmento")
    result.offset_ms = round(offsets[0] * 1000.0, 2)

    # ---- 3. escolha de streams (igual ao clássico) ----
    best_audio = merger.choose_best_audio_per_language(probes, und_lang_by_input)
    audio_langs = sorted(best_audio, key=lambda x: (x != target_iso, x == "und", x))
    log("Áudios escolhidos: " + ", ".join(
        f"{lang}<-arquivo{best_audio[lang][0] + 1}" for lang in audio_langs))
    selected_subs: list[tuple[int, dict]] = []
    for lang in audio_langs:
        selected_subs.extend(merger.pick_subs_for_lang(probes, lang, video_src=ref_input))
    chapters_src = ref_input if probes[ref_input].get("chapters") else (
        other_input if probes[other_input].get("chapters") else None)

    # ---- 4. comando ffmpeg: vídeo/áudios da referência em copy; áudios do
    #         OUTRO arquivo remontados por segmento e re-encodados ----
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostats",
           "-progress", "pipe:1", "-y", "-fflags", "+genpts",
           "-i", ref_path, "-i", oth_path]
    cmd += ["-map", f"0:v:{int(best_v['_type_index'])}"]

    # nota: aqui input 0 = referência e input 1 = outro (na ordem ref/oth),
    # diferente do clássico que usa a ordem file1/file2
    mapped = []
    for lang in audio_langs:
        src_orig, s = best_audio[lang]
        mapped.append({"lang": lang, "src_cmd": 0 if src_orig == ref_input else 1,
                       "a_idx": int(s["_type_index"]), "stream": s})

    filter_chains, filter_labels = [], {}
    for item in mapped:
        if item["src_cmd"] != 1:
            continue
        label = f"a_seg_{item['a_idx']}"
        filter_labels[item["a_idx"]] = label
        filter_chains.append(
            build_segment_chain(f"[1:a:{item['a_idx']}]", segments, offsets, label))
    if filter_chains:
        cmd += ["-filter_complex", "; ".join(filter_chains)]

    cmd += ["-c:v", "copy", "-c:a", "copy", "-c:s", "copy"]

    default_audio_out = None
    for out_i, item in enumerate(mapped):
        s = item["stream"]
        if item["src_cmd"] == 1:
            cmd += ["-map", f"[{filter_labels[item['a_idx']]}]"]
            ch = merger.channels_of(s)
            codec, bitrate = merger.filtered_codec_and_bitrate(ch)
            cmd += [f"-c:a:{out_i}", codec, f"-b:a:{out_i}", bitrate]
            note = f"áudio {item['lang']} remontado em {len(segments)} segmentos, re-encodado para {codec} {bitrate}"
            if ch > merger.AC3_MAX_CHANNELS:
                note += f" (downmix {ch}ch → 5.1)"
            result.notes.append(note)
        else:
            cmd += ["-map", f"0:a:{item['a_idx']}"]
        cmd += [f"-metadata:s:a:{out_i}", f"language={item['lang']}"]
        title = merger.clean_stream_title(s, item["lang"]) or ""
        cmd += [f"-metadata:s:a:{out_i}", f"title={title}"]
        if default_audio_out is None and target_iso and item["lang"] == target_iso:
            default_audio_out = out_i
    if default_audio_out is None:
        default_audio_out = 0
    for i in range(len(mapped)):
        cmd += [f"-disposition:a:{i}", "default" if i == default_audio_out else "0"]

    for out_s, (src_orig, s) in enumerate(selected_subs):
        src_cmd = 0 if src_orig == ref_input else 1
        cmd += ["-map", f"{src_cmd}:s:{int(s['_type_index'])}"]
        if merger.sub_needs_reencode_to_mkv(s.get("codec_name")):
            cmd += [f"-c:s:{out_s}", "subrip"]
        sub_lang = merger.canonical_lang(merger.raw_lang_of(s))
        cmd += [f"-metadata:s:s:{out_s}", f"language={sub_lang}"]
        sub_title = merger.clean_stream_title(
            s, sub_lang, from_disposition_forced=merger.is_forced(s)) or ""
        cmd += [f"-metadata:s:s:{out_s}", f"title={sub_title}"]

    cmd += ["-map_chapters", "-1" if chapters_src is None
            else ("0" if chapters_src == ref_input else "1")]
    cmd += ["-map_metadata", "0",
            "-avoid_negative_ts", "make_zero", "-max_interleave_delta", "0",
            output]

    Path(output).parent.mkdir(parents=True, exist_ok=True)
    log("Executando ffmpeg...")
    log("+ " + " ".join(cmd))
    merger._run_ffmpeg_progress(cmd, duration, on_progress)

    log(f"OK: {output}")
    return result
