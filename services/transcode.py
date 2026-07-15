"""Opções avançadas de conversão (codec/resolução/bitrate/áudios/legendas).

Este módulo é o PLANEJADOR: detecta o que o ffmpeg do servidor sabe encodar,
valida as opções vindas da UI e decide, stream a stream, o que re-encodar e
com quais argumentos. Quem executa é o merger (merge de dois arquivos) ou o
convert_single daqui (arquivo único: jobs "só original"/"só dublado" e o
atalho do merge quando o melhor vídeo já tem o áudio no idioma alvo).

Regra de ouro da validação: nunca "converter para cima". Se o bitrate pedido
é maior do que o que a fonte entrega (ajustado pela redução de resolução),
a conversão daquele stream não vale a pena:
- se era o ÚNICO motivo do re-encode, o stream fica em cópia;
- se o re-encode acontece por outro motivo (codec/resolução/canais/bit depth),
  o alvo é rebaixado para o teto estimado da fonte.

Sem as opções avançadas (convert=None em todo lugar), nada aqui roda e o
pipeline fica exatamente como sempre foi.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path

from services import merger  # o merger só importa este módulo dentro de função (sem ciclo)

# -------------------- catálogo do que oferecemos --------------------

# vídeo: id -> (label, encoders aceitos em ordem de preferência)
VIDEO_CODECS = {
    "vvc": ("VVC / H.266", ("libvvenc",)),
    "av1": ("AV1", ("libsvtav1", "libaom-av1", "librav1e")),
    "hevc": ("HEVC / H.265", ("libx265",)),
    "h264": ("H.264 / AVC", ("libx264",)),
}
# nome de codec do ffprobe -> id nosso ("o codec pedido já é o da fonte?")
_SRC_CODEC_ID = {"h264": "h264", "hevc": "hevc", "h265": "hevc", "av1": "av1",
                 "vvc": "vvc", "h266": "vvc"}

# áudio: id -> (label, encoder ffmpeg, máx. canais, lossless).
# aac limitado a estéreo de propósito: o encoder nativo embaralha a ordem dos
# canais em layouts surround (mesmo racional do filtered_codec_and_bitrate).
AUDIO_CODECS = {
    "ac3": ("AC3 (Dolby Digital)", "ac3", 6, False),
    "flac": ("FLAC (lossless)", "flac", 8, True),
    "opus": ("Opus", "libopus", 8, False),
    "vorbis": ("OGG Vorbis", "libvorbis", 8, False),
    "aac": ("AAC", "aac", 2, False),
}

# resolução: id (altura "comercial") -> teto de LARGURA. Cap por largura porque
# filme scope (2.39:1) tem altura reduzida (3840x1608 É 4K); tolerância de 8%
# porque resolução nunca é exata (DCI 4096x2160 ainda conta como 4K).
RESOLUTION_CAPS = {"4320": 7680, "2160": 3840, "1080": 1920, "720": 1280, "480": 854}
RESOLUTION_TOLERANCE = 1.08

# níveis genéricos de preset -> valor por encoder (velocidade vs compressão)
PRESET_LEVELS = ("veryfast", "fast", "default", "slow", "veryslow")
_PRESETS = {
    "libx264": {"veryfast": "veryfast", "fast": "fast", "default": "medium",
                "slow": "slow", "veryslow": "veryslow"},
    "libx265": {"veryfast": "veryfast", "fast": "fast", "default": "medium",
                "slow": "slow", "veryslow": "veryslow"},
    "libvvenc": {"veryfast": "faster", "fast": "fast", "default": "medium",
                 "slow": "slow", "veryslow": "slower"},
    "libsvtav1": {"veryfast": "10", "fast": "8", "default": "6",
                  "slow": "4", "veryslow": "2"},
    "libaom-av1": {"veryfast": "8", "fast": "6", "default": "4",
                   "slow": "3", "veryslow": "2"},
    "librav1e": {"veryfast": "10", "fast": "8", "default": "6",
                 "slow": "4", "veryslow": "2"},
}

# faixa (min, max, default) do CRF/QP por codec — a UI usa isto no slider
CRF_RANGES = {"h264": (0, 51, 21), "hevc": (0, 51, 24),
              "av1": (1, 63, 30), "vvc": (18, 45, 32)}

VIDEO_BITRATE_KBPS = (100, 150_000)
AUDIO_BITRATE_KBPS = (32, 1024)
_AUDIO_DEFAULT_KBPS = {"aac": 192, "ac3": 640, "opus": 256, "vorbis": 320}

CHANNEL_CAPS = {"stereo": 2, "surround51": 6}

# fontes lossless (regra do FLAC: lossy -> FLAC só infla o arquivo)
_LOSSLESS_AUDIO = {"truehd", "flac", "mlp", "alac"}

# libopus não aceita layouts "(side)" (5.1(side) de fontes DTS/E-AC3);
# este aformat força um layout que ele conhece antes do encode
OPUS_LAYOUT_FIX = "aformat=channel_layouts=7.1|5.1|stereo|mono"

# ISO 639-1 (original_language do TMDB) -> tags que aparecem nas faixas (639-2 B/T)
_ISO1_TO_TAGS = {
    "en": {"eng"}, "pt": {"por"}, "es": {"spa"}, "fr": {"fra", "fre"},
    "de": {"deu", "ger"}, "it": {"ita"}, "ja": {"jpn"}, "ko": {"kor"},
    "zh": {"zho", "chi", "cmn", "yue"}, "ru": {"rus"}, "hi": {"hin"},
    "ar": {"ara"}, "nl": {"nld", "dut"}, "sv": {"swe"}, "da": {"dan"},
    "no": {"nor", "nob", "nno"}, "fi": {"fin"}, "pl": {"pol"}, "tr": {"tur"},
    "th": {"tha"}, "cs": {"ces", "cze"}, "el": {"ell", "gre"}, "he": {"heb"},
    "hu": {"hun"}, "uk": {"ukr"}, "id": {"ind"}, "ta": {"tam"}, "te": {"tel"},
    "sr": {"srp"}, "ro": {"ron", "rum"}, "fa": {"fas", "per"}, "vi": {"vie"},
}


# -------------------- capacidades do servidor --------------------

_encoders_cache: frozenset[str] | None = None


def available_encoders() -> frozenset[str]:
    """Encoders do ffmpeg desta máquina (cache: não muda com o servidor de pé)."""
    global _encoders_cache
    if _encoders_cache is None:
        try:
            p = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"],
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=15)
            names = re.findall(r"^ [VAS][^ ]{5} (\S+)", p.stdout, re.M)
        except (OSError, subprocess.SubprocessError):
            names = []
        _encoders_cache = frozenset(names)
    return _encoders_cache


def _video_encoder_for(codec_id: str) -> str | None:
    enc = available_encoders()
    return next((e for e in VIDEO_CODECS[codec_id][1] if e in enc), None)


def capabilities() -> dict:
    """O que o ffmpeg DESTE servidor sabe encodar — a UI só mostra o disponível."""
    enc = available_encoders()
    video = []
    for cid, (label, _encoders) in VIDEO_CODECS.items():
        chosen = _video_encoder_for(cid)
        lo, hi, default = CRF_RANGES[cid]
        video.append({"id": cid, "label": label, "encoder": chosen,
                      "available": chosen is not None,
                      "crf": {"min": lo, "max": hi, "default": default}})
    audio = [{"id": cid, "label": label, "available": encoder in enc,
              "max_channels": maxch, "lossless": lossless,
              "default_kbps": _AUDIO_DEFAULT_KBPS.get(cid)}
             for cid, (label, encoder, maxch, lossless) in AUDIO_CODECS.items()]
    return {"video_codecs": video, "audio_codecs": audio,
            "presets": list(PRESET_LEVELS),
            "video_bitrate_kbps": list(VIDEO_BITRATE_KBPS),
            "audio_bitrate_kbps": list(AUDIO_BITRATE_KBPS)}


# -------------------- opções + validação --------------------

@dataclass
class ConvertOptions:
    video_codec: str = "keep"       # keep | vvc | av1 | hevc | h264
    preset: str = "default"         # PRESET_LEVELS
    resolution: str = "keep"        # keep | 4320 | 2160 | 1080 | 720 | 480
    quality_mode: str = "bitrate"   # bitrate | crf
    video_bitrate: int | None = None  # kbps
    crf: int | None = None
    bit_depth: str = "keep"         # keep | 10 | 8
    audio_tracks: str = "all"       # all | target (original + dublagem + desconhecidos)
    audio_codec: str = "keep"       # keep | ac3 | flac | opus | vorbis | aac
    audio_bitrate: int | None = None  # kbps por faixa
    channels: str = "keep"          # keep | surround51 | stereo
    subtitles: str = "default"      # default | all | none

    def to_dict(self) -> dict:
        return asdict(self)

    def wants_video_encode(self) -> bool:
        """O usuário pediu algo que PODE re-encodar o vídeo? (a decisão final
        depende da fonte — plan_video)."""
        return (self.video_codec != "keep" or self.resolution != "keep"
                or self.bit_depth != "keep")


def _expect(value, name: str, allowed: tuple) -> None:
    if value not in allowed:
        raise ValueError(f"{name} inválido: {value!r} (esperado: {', '.join(map(str, allowed))})")


def validate(payload: dict) -> ConvertOptions:
    """dict da UI/banco -> ConvertOptions validado. ValueError com mensagem amigável."""
    if not isinstance(payload, dict):
        raise ValueError("opções de conversão devem ser um objeto")
    known = {f for f in ConvertOptions.__dataclass_fields__}
    o = ConvertOptions(**{k: v for k, v in payload.items() if k in known})

    _expect(o.video_codec, "codec de vídeo", ("keep", *VIDEO_CODECS))
    _expect(o.preset, "preset", PRESET_LEVELS + ("default",))
    _expect(o.resolution, "resolução", ("keep", *RESOLUTION_CAPS))
    _expect(o.quality_mode, "modo de qualidade", ("bitrate", "crf"))
    _expect(o.bit_depth, "profundidade de cor", ("keep", "10", "8"))
    _expect(o.audio_tracks, "seleção de áudios", ("all", "target"))
    _expect(o.audio_codec, "codec de áudio", ("keep", *AUDIO_CODECS))
    _expect(o.channels, "canais", ("keep", *CHANNEL_CAPS))
    _expect(o.subtitles, "legendas", ("default", "all", "none"))

    if o.video_codec != "keep" and not _video_encoder_for(o.video_codec):
        raise ValueError(f"o ffmpeg do servidor não tem encoder para "
                         f"{VIDEO_CODECS[o.video_codec][0]}")
    if o.audio_codec != "keep" and AUDIO_CODECS[o.audio_codec][1] not in available_encoders():
        raise ValueError(f"o ffmpeg do servidor não tem encoder para "
                         f"{AUDIO_CODECS[o.audio_codec][0]}")

    if o.video_bitrate is not None:
        o.video_bitrate = int(o.video_bitrate)
        lo, hi = VIDEO_BITRATE_KBPS
        if not lo <= o.video_bitrate <= hi:
            raise ValueError(f"bitrate de vídeo fora da faixa {lo}–{hi} kbps")
    if o.crf is not None:
        o.crf = int(o.crf)
        codec_for_crf = o.video_codec if o.video_codec != "keep" else None
        if codec_for_crf:
            lo, hi, _d = CRF_RANGES[codec_for_crf]
            if not lo <= o.crf <= hi:
                raise ValueError(f"CRF fora da faixa {lo}–{hi} para {codec_for_crf.upper()}")
        elif not 0 <= o.crf <= 63:
            raise ValueError("CRF fora da faixa 0–63")
    if o.audio_bitrate is not None:
        o.audio_bitrate = int(o.audio_bitrate)
        lo, hi = AUDIO_BITRATE_KBPS
        if not lo <= o.audio_bitrate <= hi:
            raise ValueError(f"bitrate de áudio fora da faixa {lo}–{hi} kbps")

    if (o.wants_video_encode() and o.quality_mode == "bitrate"
            and o.video_bitrate is None):
        raise ValueError("escolha o bitrate de vídeo (ou mude para o modo CRF)")
    return o


# -------------------- plano do vídeo --------------------

@dataclass
class VideoPlan:
    encode: bool
    encoder: str | None = None
    args: list[str] = field(default_factory=list)  # -c:v/-preset/-crf|-b:v/-pix_fmt/-vf
    notes: list[str] = field(default_factory=list)


def _estimate_video_bitrate(probe: dict, vstream: dict) -> int:
    """Bitrate (bits/s) do stream de vídeo; 0 = desconhecido.

    MKV raramente declara bit_rate por stream: cai para o bitrate do container
    (ou tamanho/duração) menos os áudios (128k por faixa quando o stream de
    áudio também não declara). Estimativa concisa — só para a regra do teto.
    """
    br = merger.bit_rate_of(vstream)
    if br:
        return br
    fmt = probe.get("format") or {}
    try:
        total = int(fmt.get("bit_rate") or 0) \
            or int(int(fmt["size"]) * 8 / float(fmt["duration"]))
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return 0
    audio = sum(merger.bit_rate_of(s) or 128_000
                for s in merger.get_streams(probe, "audio"))
    return max(0, total - audio)


def plan_video(probe: dict, vstream: dict, opts: ConvertOptions) -> VideoPlan:
    """Decide se o vídeo re-encoda e monta os argumentos do encoder."""
    notes: list[str] = []
    width = int(vstream.get("width") or 0)
    height = int(vstream.get("height") or 0)
    src_name = (vstream.get("codec_name") or "").lower()
    src_id = _SRC_CODEC_ID.get(src_name)
    pix = (vstream.get("pix_fmt") or "").lower()
    src_10bit = "10le" in pix or "10be" in pix or "p10" in pix

    cap = RESOLUTION_CAPS.get(opts.resolution)
    downscale = bool(cap and width > cap * RESOLUTION_TOLERANCE)
    if cap and not downscale:
        notes.append(f"resolução mantida: a fonte ({width}x{height}) já é ≤ o alvo "
                     f"(nunca aumento resolução)")

    want_codec = opts.video_codec if opts.video_codec != "keep" else None
    depth_change = (opts.bit_depth == "10" and not src_10bit) \
        or (opts.bit_depth == "8" and src_10bit)

    if not (want_codec or downscale or depth_change):
        return VideoPlan(encode=False, notes=notes)

    # ---- teto de bitrate: nunca converter "para cima" ----
    target_kbps = opts.video_bitrate
    if opts.quality_mode == "bitrate":
        src_br = _estimate_video_bitrate(probe, vstream)
        ceiling = src_br
        if downscale and src_br:
            # a área (e o bitrate necessário) cai com o quadrado do fator de largura
            ceiling = int(src_br * (cap / width) ** 2)
        if src_br and target_kbps is not None and target_kbps * 1000 >= ceiling:
            if downscale or depth_change:
                target_kbps = max(VIDEO_BITRATE_KBPS[0], ceiling // 1000)
                notes.append(f"bitrate pedido ({opts.video_bitrate} kbps) ≥ o que a fonte "
                             f"entrega (~{ceiling // 1000} kbps) — rebaixado para o teto da fonte")
            else:
                notes.append(f"vídeo mantido sem conversão: bitrate pedido "
                             f"({opts.video_bitrate} kbps) ≥ o que a fonte já entrega "
                             f"(~{src_br // 1000} kbps) — converter só perderia qualidade")
                return VideoPlan(encode=False, notes=notes)
        if not src_br:
            notes.append("bitrate da fonte desconhecido — convertendo com o bitrate pedido")

    # ---- encoder ----
    codec_id = want_codec or src_id
    if codec_id is None or _video_encoder_for(codec_id) is None:
        # "manter codec" + downscale/bit depth numa fonte que não sabemos
        # re-gerar (vp9, mpeg2...): cai para o melhor encoder disponível
        fallback = next((c for c in ("hevc", "h264", "av1") if _video_encoder_for(c)), None)
        if fallback is None:
            raise merger.MergeError("nenhum encoder de vídeo disponível no ffmpeg do servidor")
        notes.append(f"codec da fonte ({src_name or '?'}) sem encoder no servidor — "
                     f"re-encodando em {fallback.upper()}")
        codec_id = fallback
    encoder = _video_encoder_for(codec_id)

    args = ["-c:v", encoder]
    preset = _PRESETS[encoder][opts.preset]
    if encoder == "libaom-av1":
        args += ["-cpu-used", preset, "-row-mt", "1"]
    elif encoder == "librav1e":
        args += ["-speed", preset]
    else:
        args += ["-preset", preset]

    if opts.quality_mode == "crf":
        crf = opts.crf if opts.crf is not None else CRF_RANGES[codec_id][2]
        if encoder == "libaom-av1":
            args += ["-crf", str(crf), "-b:v", "0"]
        elif encoder in ("librav1e", "libvvenc"):
            args += ["-qp", str(crf)]
        else:
            args += ["-crf", str(crf)]
        notes.append(f"vídeo re-encodado em {codec_id.upper()} ({encoder}, "
                     f"CRF {crf}, preset {opts.preset})")
    else:
        args += ["-b:v", f"{target_kbps}k"]
        notes.append(f"vídeo re-encodado em {codec_id.upper()} ({encoder}, "
                     f"{target_kbps} kbps, preset {opts.preset})")

    # ---- profundidade de cor ----
    ten_bit_out = src_10bit if opts.bit_depth == "keep" else (opts.bit_depth == "10")
    if codec_id == "h264" and ten_bit_out and opts.bit_depth != "10":
        # High10 em H.264 quase nada decodifica — só a pedido explícito
        ten_bit_out = False
        notes.append("fonte 10-bit sai em 8-bit no H.264 (High10 tem compatibilidade "
                     "péssima) — force 10-bit nas opções se quiser manter")
    args += ["-pix_fmt", "yuv420p10le" if ten_bit_out else "yuv420p"]
    if ten_bit_out and not src_10bit:
        notes.append("saída em 10-bit a partir de fonte 8-bit (reduz banding no re-encode)")

    if (vstream.get("color_transfer") or "").lower() in ("smpte2084", "arib-std-b67"):
        notes.append("fonte HDR: as propriedades de cor seguem no stream, mas metadados "
                     "HDR10+/Dolby Vision podem se perder no re-encode")

    if downscale:
        args += ["-vf", f"scale={cap}:-2"]
        notes.append(f"resolução reduzida: {width}x{height} → {cap}px de largura")

    return VideoPlan(encode=True, encoder=encoder, args=args, notes=notes)


# -------------------- plano de cada faixa de áudio --------------------

@dataclass
class AudioPlan:
    encode: bool
    encoder: str | None = None        # nome do encoder ffmpeg
    bitrate_k: int | None = None      # None = sem -b:a (flac)
    out_channels: int | None = None   # None = mantém os canais
    needs_layout_fix: bool = False    # opus multicanal precisa de aformat
    notes: list[str] = field(default_factory=list)


def _is_lossless_source(s: dict) -> bool:
    name = (s.get("codec_name") or "").lower()
    if name in _LOSSLESS_AUDIO or name.startswith("pcm_"):
        return True
    if name in ("dts", "dca"):  # só o DTS-HD MA é lossless; o core não
        profile = (s.get("profile") or "").upper()
        return "MA" in profile or "LOSSLESS" in profile
    return False


def plan_audio(s: dict, opts: ConvertOptions) -> AudioPlan:
    """Decide se UMA faixa de áudio re-encoda (codec/bitrate/canais)."""
    notes: list[str] = []
    src_ch = merger.channels_of(s) or 2
    src_codec = (s.get("codec_name") or "").lower()
    src_br = merger.bit_rate_of(s)

    want = opts.audio_codec if opts.audio_codec != "keep" else None
    caps = [c for c in (CHANNEL_CAPS.get(opts.channels),
                        AUDIO_CODECS[want][2] if want else None) if c]
    ch_cap = min(caps) if caps else None
    out_ch = ch_cap if (ch_cap and src_ch > ch_cap) else None
    if out_ch and want and AUDIO_CODECS[want][2] == out_ch and opts.channels == "keep":
        notes.append(f"downmix {src_ch}ch → {out_ch}ch pela limitação do "
                     f"{AUDIO_CODECS[want][0]}")

    if want is None:
        if out_ch is None:
            return AudioPlan(encode=False)
        # downmix pedido com "manter codec": não dá para re-gerar TrueHD/DTS —
        # usa o codec padrão do projeto (aac estéreo / ac3 5.1, como no drift)
        codec, br = merger.filtered_codec_and_bitrate(out_ch)
        notes.append(f"downmix {src_ch}ch → {out_ch}ch: re-encodado em {codec.upper()} "
                     f"(não dá para re-gerar {src_codec.upper()})")
        return AudioPlan(True, codec, int(br.rstrip("k")), out_ch, notes=notes)

    _label, encoder, _maxch, lossless = AUDIO_CODECS[want]

    if lossless:  # flac
        if not _is_lossless_source(s) and out_ch is None:
            notes.append(f"faixa {src_codec.upper()} (lossy) mantida: converter para "
                         f"FLAC só aumentaria o tamanho sem ganhar qualidade")
            return AudioPlan(encode=False, notes=notes)
        if not _is_lossless_source(s):
            notes.append("downmix com saída FLAC a partir de fonte lossy — "
                         "ganho só na contagem de canais")
        return AudioPlan(True, encoder, None, out_ch, notes=notes)

    target_k = opts.audio_bitrate or _AUDIO_DEFAULT_KBPS[want]
    # regra do usuário: bitrate pedido ≥ o da fonte -> mantém o original
    # (re-encodar lossy em bitrate maior só perde qualidade e ganha bytes)
    if out_ch is None and src_br and not _is_lossless_source(s) \
            and target_k * 1000 >= src_br:
        notes.append(f"faixa mantida: {src_codec.upper()} {src_br // 1000} kbps ≤ "
                     f"{target_k} kbps pedidos — converter só perderia qualidade")
        return AudioPlan(encode=False, notes=notes)
    if src_codec == want and out_ch is None and not src_br:
        notes.append(f"faixa já é {want.upper()} (bitrate desconhecido) — mantida")
        return AudioPlan(encode=False, notes=notes)

    fix = encoder == "libopus" and (out_ch or src_ch) > 2
    notes.append(f"re-encodada {src_codec.upper()} → {want.upper()} {target_k} kbps"
                 + (f", {out_ch}ch" if out_ch else ""))
    return AudioPlan(True, encoder, target_k, out_ch, needs_layout_fix=fix, notes=notes)


def audio_output_args(out_i: int, plan: AudioPlan, via_filter_complex: bool = False) -> list[str]:
    """Argumentos ffmpeg da faixa `out_i` conforme o plano. Quando o stream vem
    de um filter_complex (conserto de drift), o aformat do opus deve ir DENTRO
    da chain (via_filter_complex=True pula o -filter:a daqui)."""
    args = [f"-c:a:{out_i}", plan.encoder]
    if plan.bitrate_k:
        args += [f"-b:a:{out_i}", f"{plan.bitrate_k}k"]
    if plan.out_channels:
        args += [f"-ac:a:{out_i}", str(plan.out_channels)]
    if plan.needs_layout_fix and not via_filter_complex:
        args += [f"-filter:a:{out_i}", OPUS_LAYOUT_FIX]
    return args


# -------------------- filtro "apenas original + dublagem" --------------------

def allowed_langs(target_iso: str | None, original_lang: str | None) -> set[str]:
    """Línguas mantidas no modo 'apenas original e dublagem'. Sempre inclui as
    desconhecidas — por definição não se sabe o que são, então somos inclusivos."""
    keep = {"und", "unknown", ""}
    if target_iso:
        keep.add(target_iso)
    ol = (original_lang or "").strip().lower()
    if ol:
        keep |= _ISO1_TO_TAGS.get(ol, set())
        keep.add(ol)  # cobre tag de 2 letras crua e línguas fora da tabela
        keep.add(merger.canonical_lang(ol))
    return keep


# -------------------- conversão de arquivo único --------------------

def convert_single(src: str, output: str, opts: ConvertOptions,
                   target_lang: str | None = None, original_lang: str | None = None,
                   log=print, on_progress=None) -> "merger.MergeResult":
    """Converte UM arquivo conforme as opções: jobs 'só original'/'só dublado'
    e o atalho do merge quando o melhor vídeo já tem o áudio no idioma alvo.

    Se o plano inteiro der em cópia (nada a converter), entrega por
    hardlink/cópia, como o pipeline sempre fez.
    """
    merger._check_tools()
    if not Path(src).exists():
        raise merger.MergeError(f"arquivo não existe: {src}")
    probe = merger.ffprobe_json(src)
    merger.annotate_type_indexes(probe)

    vstreams = [s for s in merger.get_streams(probe, "video")
                if (s.get("disposition") or {}).get("attached_pic") != 1]
    if not vstreams:
        raise merger.MergeError(f"nenhuma stream de vídeo em {src}")
    best_v = max(vstreams, key=merger.video_score)
    vplan = plan_video(probe, best_v, opts)

    target_iso = merger.canonical_lang(
        merger.LANG_ISO.get(target_lang, target_lang)) if target_lang else None
    keep_langs = allowed_langs(target_iso, original_lang) \
        if opts.audio_tracks == "target" else None

    audios = merger.get_streams(probe, "audio")
    kept: list[tuple[dict, AudioPlan]] = []
    dropped: list[str] = []
    for s in audios:
        lang = merger.canonical_lang(merger.raw_lang_of(s))
        if keep_langs is not None and lang not in keep_langs:
            dropped.append(lang)
            continue
        kept.append((s, plan_audio(s, opts)))
    if audios and not kept:  # o filtro nunca pode zerar os áudios
        kept = [(s, plan_audio(s, opts)) for s in audios]
        dropped = []

    all_subs = merger.get_streams(probe, "subtitle")
    subs = [] if opts.subtitles == "none" else all_subs

    result = merger.MergeResult(output=output)
    result.notes += vplan.notes
    if dropped:
        result.notes.append("faixas de áudio removidas (apenas original + dublagem "
                            "+ desconhecidas): " + ", ".join(dropped))
    for s, plan in kept:
        lang = merger.canonical_lang(merger.raw_lang_of(s))
        result.notes += [f"áudio {lang or 'und'}: {n}" for n in plan.notes]

    nothing_to_do = (not vplan.encode and not dropped
                     and all(not p.encode for _s, p in kept)
                     and not (opts.subtitles == "none" and all_subs))
    if nothing_to_do:
        out_path = Path(output).with_suffix(Path(src).suffix)
        result.output = str(out_path)
        result.linked = True
        result.notes.append("nada a converter com as opções escolhidas — "
                            "arquivo entregue por hardlink/cópia")
        for n in result.notes:
            log(n)
        merger._link_or_copy(Path(src), out_path, result.notes)
        return result

    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostats",
           "-progress", "pipe:1", "-y", "-fflags", "+genpts", "-i", src,
           "-map", f"0:v:{int(best_v['_type_index'])}"]
    cmd += vplan.args if vplan.encode else ["-c:v", "copy"]

    for out_i, (s, plan) in enumerate(kept):
        cmd += ["-map", f"0:a:{int(s['_type_index'])}"]
        cmd += audio_output_args(out_i, plan) if plan.encode else [f"-c:a:{out_i}", "copy"]

    for out_s, s in enumerate(subs):
        cmd += ["-map", f"0:s:{int(s['_type_index'])}"]
        cmd += [f"-c:s:{out_s}",
                "subrip" if merger.sub_needs_reencode_to_mkv(s.get("codec_name")) else "copy"]

    # anexos (fontes de legendas ASS etc.) sobrevivem no MKV de saída
    cmd += ["-map", "0:t?", "-c:t", "copy",
            "-map_chapters", "0", "-map_metadata", "0",
            "-avoid_negative_ts", "make_zero", "-max_interleave_delta", "0", output]

    Path(output).parent.mkdir(parents=True, exist_ok=True)
    for n in result.notes:
        log(n)
    log("Executando ffmpeg...")
    log("+ " + " ".join(cmd))
    merger._run_ffmpeg_progress(cmd, merger._duration_of(probe), on_progress)
    log(f"OK: {output}")
    return result
