"""Merge interno de dois arquivos de video.

- escolhe o melhor VIDEO entre os 2 (resolucao, 10-bit, bitrate, codec)
- se o arquivo de melhor video JA TEM audio no idioma alvo: pula o merge e
  cria um hardlink no destino (fallback: copia — nunca symlink)
- escolhe o melhor AUDIO por LINGUA entre os 2 arquivos
- inclui LEGENDAS apenas nas linguas em que ha audio
- copia CAPITULOS do arquivo de melhor video (ou do outro, se so ele tiver)
- mede o OFFSET entre os arquivos via correlacao GCC-PHAT
- ajusta somente os audios vindos do "outro" arquivo via -filter_complex
  (aresample=async=1:first_pts=0, asetpts, adelay/atrim) — esses audios sao
  re-encodados (AAC; AC3 se >6 canais); o resto e stream copy
- flags plex-friendly: -fflags +genpts, -avoid_negative_ts make_zero,
  -max_interleave_delta 0; legendas mov_text/tx3g viram subrip no MKV

Requer ffmpeg + ffprobe no PATH e numpy.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import wave
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

# ---------- parametros do alinhamento ----------
ALIGN_SR = 8000
ALIGN_START = 30.0        # pula intros/logos
ALIGN_DURATION = 300.0    # janela de analise (s)
MAX_OFFSET_SECONDS = 60.0
ZERO_THRESHOLD_MS = 0.5

# ---------- codec dos audios filtrados ----------
AAC_BITRATE_STEREO = "192k"
AAC_BITRATE_SURROUND = "384k"
AAC_MAX_CHANNELS = 6
AC3_BITRATE = "640k"

LANG_ISO = {"pt": "por", "es": "spa", "it": "ita", "de": "deu", "fr": "fra", "en": "eng"}
LANG_ALIASES = {
    "por": {"pt", "por", "pob", "pt-br", "ptbr", "pb", "bra"},
    "spa": {"es", "spa", "esp", "es-la", "lat", "es-419"},
    "ita": {"it", "ita"},
    "deu": {"de", "deu", "ger"},
    "fra": {"fr", "fra", "fre"},
    "eng": {"en", "eng"},
}

VIDEO_CODEC_WEIGHT = {
    "av1": 50, "hevc": 40, "h265": 40, "h264": 30, "vp9": 25, "mpeg2video": 10, "mpeg4": 8,
}
AUDIO_CODEC_WEIGHT = {
    "truehd": 100, "flac": 95, "pcm_s24le": 92, "pcm_s16le": 90,
    "dca": 85, "eac3": 75, "ac3": 65, "opus": 60, "aac": 55, "vorbis": 50, "mp3": 45,
}
SUB_CODEC_WEIGHT = {
    "ass": 60, "ssa": 58, "subrip": 55, "webvtt": 52,
    "hdmv_pgs_subtitle": 45, "dvd_subtitle": 40,
    "mov_text": 10, "tx3g": 10,
}


@dataclass
class MergeResult:
    output: str
    linked: bool = False  # True quando pulou o merge e só hardlinkou/copiou
    offset_ms: float | None = None
    notes: list[str] = field(default_factory=list)


class MergeError(RuntimeError):
    pass


# -------------------- ffprobe helpers --------------------

def _check_tools():
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            raise MergeError(f"{tool} não encontrado no PATH")


def ffprobe_json(path: str) -> dict:
    p = subprocess.run(
        ["ffprobe", "-hide_banner", "-loglevel", "error", "-print_format", "json",
         "-show_format", "-show_streams", "-show_chapters", path],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    if p.returncode != 0:
        raise MergeError(f"ffprobe falhou em '{path}': {p.stderr.strip()}")
    return json.loads(p.stdout)


def annotate_type_indexes(probe: dict) -> None:
    counters: dict[str, int] = defaultdict(int)
    for s in probe.get("streams", []):
        t = s.get("codec_type", "unknown")
        s["_type_index"] = counters[t]
        counters[t] += 1


def get_streams(probe: dict, codec_type: str) -> list[dict]:
    return [s for s in probe.get("streams", []) if s.get("codec_type") == codec_type]


def tags_of(s: dict) -> dict:
    return s.get("tags") or {}


def raw_lang_of(s: dict) -> str:
    lang = (tags_of(s).get("language") or "und").strip().lower()
    return lang or "und"


def canonical_lang(tag: str) -> str:
    """Normaliza aliases (pob/pt-br -> por, ger -> deu, ...)."""
    for canon, aliases in LANG_ALIASES.items():
        if tag == canon or tag in aliases:
            return canon
    return tag


def bit_rate_of(s: dict) -> int:
    try:
        return int(s.get("bit_rate") or 0)
    except (TypeError, ValueError):
        return 0


def channels_of(s: dict) -> int:
    try:
        return int(s.get("channels") or 0)
    except (TypeError, ValueError):
        return 0


def is_forced(s: dict) -> bool:
    return (s.get("disposition") or {}).get("forced") == 1


def is_default(s: dict) -> bool:
    return (s.get("disposition") or {}).get("default") == 1


# -------------------- heuristicas de "melhor" --------------------

def video_score(s: dict) -> tuple:
    pixels = int(s.get("width") or 0) * int(s.get("height") or 0)
    pix_fmt = (s.get("pix_fmt") or "").lower()
    ten_bit = 1 if ("p10" in pix_fmt or "10le" in pix_fmt) else 0
    cw = VIDEO_CODEC_WEIGHT.get((s.get("codec_name") or "").lower(), 0)
    return (pixels, ten_bit, bit_rate_of(s), cw)


def audio_score(s: dict) -> tuple:
    cw = AUDIO_CODEC_WEIGHT.get((s.get("codec_name") or "").lower(), 0)
    return (cw, channels_of(s), int(s.get("sample_rate") or 0), bit_rate_of(s))


def subtitle_score(s: dict) -> tuple:
    cw = SUB_CODEC_WEIGHT.get((s.get("codec_name") or "").lower(), 0)
    return (1 if is_forced(s) else 0, 1 if is_default(s) else 0, cw)


def sub_needs_reencode_to_mkv(codec_name: str) -> bool:
    return (codec_name or "").lower() in {"mov_text", "tx3g"}


# -------------------- offset (GCC-PHAT) --------------------

def _extract_mono_wav(input_path: str, a_type_index: int, output_wav: str,
                      start: float = ALIGN_START):
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           "-ss", str(start), "-i", input_path,
           "-map", f"0:a:{a_type_index}", "-vn", "-sn",
           "-ac", "1", "-ar", str(ALIGN_SR), "-acodec", "pcm_s16le",
           "-t", str(ALIGN_DURATION), output_wav]
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if p.returncode != 0:
        raise MergeError(f"falha ao extrair áudio de '{input_path}': {p.stderr.strip()}")


def _read_wav(path: str) -> np.ndarray:
    with wave.open(path, "rb") as wf:
        raw = wf.readframes(wf.getnframes())
    if not raw:
        raise MergeError(f"WAV vazio: {path}")
    return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0


def gcc_phat_delay_seconds(sig: np.ndarray, ref: np.ndarray, fs: int,
                           max_tau: float | None = MAX_OFFSET_SECONDS) -> float:
    """tau > 0 => sig ATRASADO vs ref; tau < 0 => sig ADIANTADO."""
    sig = sig - np.mean(sig)
    ref = ref - np.mean(ref)
    sig /= (np.std(sig) + 1e-12)
    ref /= (np.std(ref) + 1e-12)

    n = sig.size + ref.size
    nfft = 1 << (n - 1).bit_length()
    R = np.fft.rfft(sig, nfft) * np.conj(np.fft.rfft(ref, nfft))
    R /= np.maximum(np.abs(R), 1e-15)
    cc = np.fft.irfft(R, nfft)

    max_shift = int(nfft / 2)
    if max_tau is not None:
        max_shift = min(max_shift, int(fs * max_tau))
    cc = np.concatenate((cc[-max_shift:], cc[:max_shift + 1]))
    shift = int(np.argmax(np.abs(cc)) - max_shift)
    return shift / float(fs)


# -------------------- selecao de streams --------------------

def choose_best_video(probes: list[dict]) -> tuple[int, dict]:
    best = None
    for i, pr in enumerate(probes):
        for s in get_streams(pr, "video"):
            if (s.get("disposition") or {}).get("attached_pic") == 1:
                continue
            sc = video_score(s)
            if best is None or sc > best[0]:
                best = (sc, i, s)
    if best is None:
        raise MergeError("nenhuma stream de vídeo encontrada")
    return best[1], best[2]


def _lang_of_stream(s: dict, input_index: int, und_lang_by_input: dict[int, str]) -> str:
    lang = canonical_lang(raw_lang_of(s))
    if lang == "und" and input_index in und_lang_by_input:
        return und_lang_by_input[input_index]
    return lang


def choose_best_audio_per_language(probes: list[dict],
                                   und_lang_by_input: dict[int, str]) -> dict[str, tuple[int, dict]]:
    grouped: dict[str, list[tuple[int, dict]]] = {}
    for i, pr in enumerate(probes):
        for s in get_streams(pr, "audio"):
            grouped.setdefault(_lang_of_stream(s, i, und_lang_by_input), []).append((i, s))
    return {lang: max(cands, key=lambda it: audio_score(it[1]))
            for lang, cands in grouped.items()}


def pick_subs_for_lang(probes: list[dict], lang: str, video_src: int) -> list[tuple[int, dict]]:
    """Forcada + completa para a lingua, preferindo o arquivo do video."""
    def collect(i: int) -> list[dict]:
        return [s for s in get_streams(probes[i], "subtitle")
                if canonical_lang(raw_lang_of(s)) == lang]

    pref, other = collect(video_src), collect(1 - video_src)

    def pick(cands: list[dict], forced: bool) -> dict | None:
        pool = [s for s in cands if is_forced(s) == forced]
        return max(pool, key=subtitle_score) if pool else None

    out: list[tuple[int, dict]] = []
    forced_s = pick(pref, True) or pick(other, True)
    if forced_s:
        out.append((video_src if forced_s in pref else 1 - video_src, forced_s))
    full_s = pick(pref, False) or pick(other, False)
    if full_s:
        src = video_src if full_s in pref else 1 - video_src
        if not out or out[0][0] != src or out[0][1]["_type_index"] != full_s["_type_index"]:
            out.append((src, full_s))
    return out


def _duration_of(probe: dict) -> float:
    try:
        return float((probe.get("format") or {}).get("duration") or 0)
    except (TypeError, ValueError):
        return 0.0


def _measure_offset(ref_path: str, ref_a: int, oth_path: str, oth_a: int,
                    start: float) -> float:
    """Offset (s) numa janela de ALIGN_DURATION a partir de `start`."""
    with tempfile.TemporaryDirectory(prefix="merge_align_") as td:
        wref, woth = str(Path(td) / "ref.wav"), str(Path(td) / "oth.wav")
        _extract_mono_wav(ref_path, ref_a, wref, start)
        _extract_mono_wav(oth_path, oth_a, woth, start)
        return gcc_phat_delay_seconds(_read_wav(woth), _read_wav(wref), ALIGN_SR)


def choose_alignment_pair(probes: list[dict], ref_input: int) -> tuple[int, int]:
    """Indices (a:N) dos audios usados para medir o offset — de preferencia da mesma lingua."""
    other = 1 - ref_input
    aud_ref = get_streams(probes[ref_input], "audio")
    aud_oth = get_streams(probes[other], "audio")
    if not aud_ref or not aud_oth:
        raise MergeError("não achei áudio em um dos arquivos para medir o offset")

    by_lang_ref, by_lang_oth = defaultdict(list), defaultdict(list)
    for s in aud_ref:
        by_lang_ref[canonical_lang(raw_lang_of(s))].append(s)
    for s in aud_oth:
        by_lang_oth[canonical_lang(raw_lang_of(s))].append(s)

    common = sorted(set(by_lang_ref) & set(by_lang_oth) - {"und"})
    if common:
        def best_pair(lang):
            r = max(by_lang_ref[lang], key=audio_score)
            o = max(by_lang_oth[lang], key=audio_score)
            return (audio_score(r)[0] + audio_score(o)[0], r, o)
        _, rbest, obest = max((best_pair(l) for l in common), key=lambda x: x[0])
        return int(rbest["_type_index"]), int(obest["_type_index"])

    rbest = max(aud_ref, key=audio_score)
    obest = max(aud_oth, key=audio_score)
    return int(rbest["_type_index"]), int(obest["_type_index"])


# -------------------- filter / codec dos audios ajustados --------------------

def filtered_codec_and_bitrate(channels: int) -> tuple[str, str]:
    ch = channels or 2
    if ch > AAC_MAX_CHANNELS:
        return ("ac3", AC3_BITRATE)
    return ("aac", AAC_BITRATE_STEREO if ch <= 2 else AAC_BITRATE_SURROUND)


def build_audio_fix_chain(input_spec: str, tau_s: float) -> str:
    """PTS limpo + adelay (adiantado) ou atrim (atrasado)."""
    base = f"{input_spec}aresample=async=1:first_pts=0,asetpts=N/SR/TB"
    if abs(tau_s * 1000.0) < ZERO_THRESHOLD_MS:
        return base
    if tau_s < 0:
        return f"{base},adelay={int(round(abs(tau_s) * 1000))}:all=1"
    return f"{base},atrim=start={tau_s:.6f},asetpts=N/SR/TB"


# -------------------- link quando o merge e desnecessario --------------------

def _link_or_copy(src: Path, dst: Path, notes: list[str]):
    """Coloca `src` em `dst` sem re-encodar: hardlink por padrão, cópia se falhar.

    Nada de symlink — hardlink é o preferido (mesmo inode, sem duplicar bytes;
    não quebra se o original for movido). Se não der (volumes diferentes, FS sem
    suporte a hardlink), cai para cópia real.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists() or dst.is_symlink():
        dst.unlink()
    try:
        os.link(src, dst)
        notes.append(f"hardlink criado: {dst}")
        return
    except OSError as e:
        notes.append(f"hardlink falhou ({e}); copiando arquivo")
    try:
        shutil.copy2(src, dst)
    except OSError as e:
        # shutil.copy2 usa os.sendfile/copy_file_range no Linux, que falham com
        # EINVAL em alguns filesystems (drvfs/9p do WSL em /mnt/*, SMB...).
        # Refaz a cópia em blocos com read/write puro, que funciona em qualquer FS.
        notes.append(f"cópia rápida falhou ({e}); copiando em modo compatível")
        with open(src, "rb") as fsrc, open(dst, "wb") as fdst:
            shutil.copyfileobj(fsrc, fdst, length=16 * 1024 * 1024)
        try:
            shutil.copystat(src, dst)
        except OSError:
            pass  # metadados (mtime/permissões) são melhor-esforço
    notes.append(f"cópia criada: {dst}")


# -------------------- progresso do ffmpeg (-progress pipe:1) --------------------

def _hms_to_seconds(text: str) -> float:
    m = re.match(r"(\d+):(\d+):(\d+(?:\.\d+)?)", text or "")
    if not m:
        return 0.0
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))


def _parse_progress_block(raw: dict, duration_s: float) -> dict:
    """Bloco chave=valor do -progress -> dict amigável para a UI."""
    # out_time_us e out_time_ms são AMBOS microssegundos (quirk histórico do ffmpeg)
    out_us = raw.get("out_time_us") or raw.get("out_time_ms") or ""
    if out_us.lstrip("-").isdigit():
        out_s = max(0.0, int(out_us) / 1_000_000)
    else:
        out_s = _hms_to_seconds(raw.get("out_time", ""))

    try:
        speed = float((raw.get("speed") or "").rstrip("x") or 0)
    except ValueError:
        speed = 0.0
    try:
        fps = float(raw.get("fps") or 0)
    except ValueError:
        fps = 0.0
    m = re.match(r"([\d.]+)\s*kbits/s", raw.get("bitrate") or "")
    bitrate = int(float(m.group(1)) * 1000) if m else 0  # bits/s
    size = int(raw["total_size"]) if (raw.get("total_size") or "").isdigit() else 0

    pct = min(100.0, out_s / duration_s * 100) if duration_s > 0 else 0.0
    eta = (duration_s - out_s) / speed if (speed > 0 and duration_s > out_s) else None
    return {"pct": round(pct, 1), "out_s": round(out_s, 1),
            "duration_s": round(duration_s, 1), "size": size, "bitrate": bitrate,
            "speed": round(speed, 2), "fps": round(fps, 1),
            "eta": round(eta) if eta is not None else None}


def _run_ffmpeg_progress(cmd: list[str], duration_s: float,
                         on_progress=None) -> None:
    """Roda o ffmpeg lendo o stream do -progress pipe:1 e reportando via callback.

    stderr é drenado numa thread (para não travar o pipe) e usado na mensagem
    de erro se o ffmpeg falhar.
    """
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace", bufsize=1)
    stderr_lines: list[str] = []
    t = threading.Thread(target=lambda: stderr_lines.extend(proc.stderr), daemon=True)
    t.start()

    block: dict = {}
    for line in proc.stdout:
        line = line.strip()
        if not line or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key != "progress":
            block[key] = value
            continue
        # "progress=continue|end" fecha um bloco de status
        if on_progress:
            info = _parse_progress_block(block, duration_s)
            if value == "end":
                info["pct"] = 100.0
                info["eta"] = 0
            on_progress(info)
        block = {}

    proc.wait()
    t.join(timeout=5)
    if proc.returncode != 0:
        tail = "".join(stderr_lines)[-3000:]
        raise MergeError(f"ffmpeg falhou (código {proc.returncode}):\n{tail}")


# -------------------- merge principal --------------------

def merge(file1: str, file2: str, output: str, target_lang: str | None = None,
          file2_is_target_dub: bool = True, log=print, on_progress=None) -> MergeResult:
    """Faz o merge de file1+file2 em `output`.

    target_lang: codigo curto (pt/es/...) ou ISO (por/spa/...) do idioma desejado.
    file2_is_target_dub: trata audios "und" do file2 como sendo do idioma alvo.
    on_progress: callback(dict) chamado ~2x/s durante o ffmpeg com
        {pct, out_s, duration_s, size, bitrate, speed, fps, eta}.
    """
    _check_tools()
    for f in (file1, file2):
        if not Path(f).exists():
            raise MergeError(f"arquivo não existe: {f}")

    result = MergeResult(output=output)
    target_iso = canonical_lang(LANG_ISO.get(target_lang, target_lang)) if target_lang else None

    probes = [ffprobe_json(file1), ffprobe_json(file2)]
    for pr in probes:
        annotate_type_indexes(pr)

    ref_input, best_v = choose_best_video(probes)
    other_input = 1 - ref_input
    ref_path = file1 if ref_input == 0 else file2
    oth_path = file2 if ref_input == 0 else file1
    log(f"Melhor vídeo: {ref_path}")

    und_lang_by_input: dict[int, str] = {}
    if target_iso and file2_is_target_dub:
        und_lang_by_input[1] = target_iso

    # ---- atalho: o arquivo de melhor video ja tem o audio alvo? ----
    if target_iso:
        ref_langs = {_lang_of_stream(s, ref_input, und_lang_by_input)
                     for s in get_streams(probes[ref_input], "audio")}
        if target_iso in ref_langs:
            log(f"O arquivo de melhor vídeo já tem áudio '{target_iso}' — pulando merge, criando hardlink.")
            out_path = Path(output).with_suffix(Path(ref_path).suffix)
            _link_or_copy(Path(ref_path), out_path, result.notes)
            result.output = str(out_path)
            result.linked = True
            for n in result.notes:
                log(n)
            return result

    # ---- melhor audio por lingua e legendas correspondentes ----
    best_audio = choose_best_audio_per_language(probes, und_lang_by_input)
    audio_langs = sorted(best_audio, key=lambda x: (x != target_iso, x == "und", x))
    log("Áudios escolhidos: " + ", ".join(
        f"{lang}<-arquivo{best_audio[lang][0] + 1}" for lang in audio_langs))

    selected_subs: list[tuple[int, dict]] = []
    for lang in audio_langs:
        selected_subs.extend(pick_subs_for_lang(probes, lang, video_src=ref_input))

    chapters_src = ref_input if probes[ref_input].get("chapters") else (
        other_input if probes[other_input].get("chapters") else None)

    # ---- offset (medido em duas janelas para validar o alinhamento) ----
    ref_align_a, oth_align_a = choose_alignment_pair(probes, ref_input)
    tau_1 = _measure_offset(ref_path, ref_align_a, oth_path, oth_align_a, ALIGN_START)
    tau_s = tau_1
    log(f"Offset na janela 1 ({ALIGN_START:.0f}s): {tau_1 * 1000:+.1f} ms")

    duration = min(d for d in (_duration_of(probes[0]), _duration_of(probes[1])) if d > 0) \
        if (_duration_of(probes[0]) and _duration_of(probes[1])) else 0.0
    if duration > ALIGN_START + 2 * ALIGN_DURATION + 60:
        start2 = min(max(ALIGN_START + ALIGN_DURATION, duration * 0.6),
                     duration - ALIGN_DURATION - 30)
        tau_2 = _measure_offset(ref_path, ref_align_a, oth_path, oth_align_a, start2)
        log(f"Offset na janela 2 ({start2:.0f}s): {tau_2 * 1000:+.1f} ms")
        if abs(tau_1 - tau_2) <= 0.15:
            tau_s = (tau_1 + tau_2) / 2
            log("Offsets consistentes nas duas janelas — alinhamento validado ✓")
        else:
            warn = (f"⚠️ Offsets divergem entre o início ({tau_1 * 1000:+.0f} ms) e o meio "
                    f"({tau_2 * 1000:+.0f} ms) do filme — possível corte diferente ou drift; "
                    f"o áudio pode dessincronizar. Usando o offset do início.")
            result.notes.append(warn)
            log(warn)
    else:
        log("Filme curto demais para validar o offset numa segunda janela — usando só a primeira.")

    result.offset_ms = round(tau_s * 1000.0, 2)
    log(f"Offset aplicado: {result.offset_ms:+.2f} ms "
        + (f"({'arquivo 2 atrasado, atrim' if tau_s > 0 else 'arquivo 2 adiantado, adelay'})"
           if abs(result.offset_ms) >= ZERO_THRESHOLD_MS else "(~0, só conserto de PTS)"))

    # ---- comando ffmpeg ----
    # -progress pipe:1: stream chave=valor no stdout para a barra de progresso
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostats",
           "-progress", "pipe:1", "-y",
           "-fflags", "+genpts", "-i", ref_path, "-i", oth_path]

    cmd += ["-map", f"0:v:{int(best_v['_type_index'])}"]

    mapped = []
    for lang in audio_langs:
        src_orig, s = best_audio[lang]
        mapped.append({"lang": lang, "src_cmd": 0 if src_orig == ref_input else 1,
                       "a_idx": int(s["_type_index"]), "stream": s})

    # audios do input 1 passam pelo conserto de PTS/offset (e re-encodam)
    filter_chains, filter_labels = [], {}
    for item in mapped:
        if item["src_cmd"] != 1:
            continue
        label = f"a_fix_{item['a_idx']}"
        filter_labels[item["a_idx"]] = label
        filter_chains.append(build_audio_fix_chain(f"[1:a:{item['a_idx']}]", tau_s) + f"[{label}]")
    if filter_chains:
        cmd += ["-filter_complex", "; ".join(filter_chains)]

    cmd += ["-c:v", "copy", "-c:a", "copy", "-c:s", "copy"]

    default_audio_out = None
    for out_i, item in enumerate(mapped):
        s = item["stream"]
        if item["src_cmd"] == 1:
            cmd += ["-map", f"[{filter_labels[item['a_idx']]}]"]
            codec, bitrate = filtered_codec_and_bitrate(channels_of(s))
            cmd += [f"-c:a:{out_i}", codec, f"-b:a:{out_i}", bitrate]
            result.notes.append(f"áudio {item['lang']} re-encodado para {codec} {bitrate}")
        else:
            cmd += ["-map", f"0:a:{item['a_idx']}"]
        cmd += [f"-metadata:s:a:{out_i}", f"language={item['lang']}"]
        title = (tags_of(s).get("title") or "").strip()
        if title:
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
        if sub_needs_reencode_to_mkv(s.get("codec_name")):
            cmd += [f"-c:s:{out_s}", "subrip"]
        cmd += [f"-metadata:s:s:{out_s}", f"language={canonical_lang(raw_lang_of(s))}"]

    cmd += ["-map_chapters", "-1" if chapters_src is None else str(0 if chapters_src == ref_input else 1)]
    cmd += ["-map_metadata", "0",
            "-avoid_negative_ts", "make_zero", "-max_interleave_delta", "0",
            output]

    Path(output).parent.mkdir(parents=True, exist_ok=True)
    log("Executando ffmpeg...")
    log("+ " + " ".join(cmd))
    # duracao esperada da saida = duracao do arquivo de referencia (video em copy)
    out_duration = _duration_of(probes[ref_input]) or duration
    _run_ffmpeg_progress(cmd, out_duration, on_progress)

    log(f"OK: {output}")
    return result
