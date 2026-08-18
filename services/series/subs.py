"""Legendas EXTERNAS do torrent (srt/ass/ssa/vtt) → dentro do MKV final.

Fluxo:
1. `find_for_episode` acha os arquivos de legenda do episódio dentro do
   diretório do torrent (mesmo prefixo do vídeo, SxxEyy no nome ou na pasta,
   ou qualquer legenda quando o torrent tem um vídeo só).
2. `guess_language` lê o idioma do NOME (pt-BR, por, English, "Português"...)
   e, sem pista, do CONTEÚDO (stopwords pt/en/es); senão "und".
3. `load_cues` normaliza tudo para cues SRT em UTF-8 (ass/ssa/vtt passam
   pelo ffmpeg — perde estilo, ganha remapeabilidade).
4. `remap` aplica uma função de tempo por cue: offset constante do caminho
   rápido ou a EDL do alinhador (legenda do lado DUBLADO segue os mesmos
   cortes que o áudio dublado; cue em trecho descartado some).
5. `plan` deduplica: legenda externa com (idioma, forçada) já presente como
   TEXTO embutido no MKV é redundante; entre os dois lados, o do ORIGINAL
   ganha (não precisa de remapeamento). Legenda embutida só em BITMAP
   (PGS/VobSub) não conta como duplicata — texto é outra coisa.
6. `mux` reescreve o MKV com as faixas novas (stream copy total, mesmo
   -max_interleave_delta 0 do resto do projeto).
"""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
from pathlib import Path

from services import merger
from services.series import parse

TEXT_EXT = {".srt", ".ass", ".ssa", ".vtt"}
TEXT_CODECS = {"subrip", "srt", "ass", "ssa", "webvtt", "mov_text", "text"}

_TS_RE = re.compile(
    r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})")

# pistas de idioma no nome do arquivo/pasta (minúsculas, tokens separados)
_LANG_TOKENS = {
    "por": {"pt", "por", "pob", "ptbr", "pt-br", "pt_br", "brazilian", "brazil",
            "portuguese", "portugues", "português", "brasileiro", "bra", "pb"},
    "eng": {"en", "eng", "english", "ingles", "inglês", "en-us", "en_us", "en-gb"},
    "spa": {"es", "spa", "esp", "spanish", "espanol", "español", "latino",
            "es-la", "es_la", "es-419", "castellano", "lat"},
    "ita": {"it", "ita", "italian", "italiano"},
    "deu": {"de", "deu", "ger", "german", "deutsch"},
    "fra": {"fr", "fra", "fre", "french", "francais", "français"},
    "jpn": {"ja", "jpn", "japanese"},
}
# stopwords bem distintas por idioma (conteúdo) — só para o fallback
_STOPWORDS = {
    "por": {"não", "você", "está", "isso", "para", "com", "uma", "ele", "ela",
            "muito", "aqui", "também", "mas", "eu", "nós", "obrigado", "sim"},
    "eng": {"the", "you", "and", "that", "what", "this", "with", "have", "not",
            "your", "just", "know", "don't", "it's", "i'm", "was", "yeah"},
    "spa": {"que", "el", "los", "las", "usted", "está", "pero", "por", "para",
            "con", "una", "esto", "muy", "aquí", "también", "gracias", "sí"},
}


# -------------------- localizar --------------------

def refs_in_path(rel: str) -> list[tuple[int, int]]:
    """SxxEyy no nome do arquivo; se não houver, nas pastas (Subs/S01E01/...)."""
    parts = rel.replace("\\", "/").split("/")
    refs = parse.parse_episode_refs(parts[-1])
    if refs:
        return refs
    for d in reversed(parts[:-1]):
        refs = parse.parse_episode_refs(d)
        if refs:
            return refs
    return []


def find_for_episode(root, season: int, episode: int,
                     video_path: str | None = None) -> list[Path]:
    """Legendas de texto do episódio dentro do torrent (ordem estável)."""
    p = Path(root)
    if p.is_file():
        p = p.parent
    if not p.is_dir():
        return []
    subs = sorted(f for f in p.rglob("*")
                  if f.is_file() and f.suffix.lower() in TEXT_EXT
                  and "sample" not in f.name.lower())
    if not subs:
        return []
    vids = [f for f in p.rglob("*")
            if f.is_file() and f.suffix.lower() in _video_ext()]
    vstem = Path(video_path).stem.lower() if video_path else None
    out: list[Path] = []
    for f in subs:
        rel = str(f.relative_to(p))
        refs = refs_in_path(rel)
        if refs:
            if (season, episode) in refs:
                out.append(f)
            continue
        # sem referência: mesmo prefixo do vídeo (Video.pt.srt) ou torrent
        # com um vídeo só (tudo é dele)
        if vstem and f.stem.lower().startswith(vstem):
            out.append(f)
        elif len(vids) <= 1:
            out.append(f)
    return out


def find_for_movie(root, video_path: str | None = None) -> list[Path]:
    """Legendas de texto de um torrent de FILME: todas quando há um vídeo só;
    com extras no torrent, as com o prefixo do filme ou numa pasta Subs/."""
    p = Path(root)
    if p.is_file():
        p = p.parent
    if not p.is_dir():
        return []
    subs = sorted(f for f in p.rglob("*")
                  if f.is_file() and f.suffix.lower() in TEXT_EXT
                  and "sample" not in f.name.lower())
    if not subs:
        return []
    vids = [f for f in p.rglob("*")
            if f.is_file() and f.suffix.lower() in _video_ext()
            and "sample" not in f.name.lower()]
    if len(vids) <= 1:
        return subs
    vstem = Path(video_path).stem.lower() if video_path else None
    out = []
    for f in subs:
        in_subs_dir = any(d.name.lower() in ("subs", "subtitles", "legendas")
                          for d in f.relative_to(p).parents)
        if (vstem and f.stem.lower().startswith(vstem)) or in_subs_dir:
            out.append(f)
    return out


def _video_ext() -> set[str]:
    from services import jobs
    return jobs.VIDEO_EXTENSIONS


# -------------------- idioma / sabor --------------------

def _split(text: str) -> list[str]:
    return [t for t in re.split(r"[\s._\-\[\]()]+", text.lower()) if t]


def _tokens(path: Path, video_path: str | None) -> list[str]:
    """Tokens do NOME da legenda (sem o prefixo igual ao do vídeo)."""
    stem = path.stem
    if video_path:
        vstem = Path(video_path).stem
        if stem.lower().startswith(vstem.lower()):
            stem = stem[len(vstem):]  # só o sufixo (".pt-BR", "_forced")
    return _split(stem)


def _lang_from_tokens(toks: list[str]) -> str | None:
    # tokens compostos (pt-br) já foram quebrados: reconstrói pares
    pairs = {f"{a}-{b}" for a, b in zip(toks, toks[1:])}
    for lang, keys in _LANG_TOKENS.items():
        if any(t in keys for t in toks) or pairs & keys:
            return lang
    return None


def guess_language(path, video_path: str | None = None,
                   fallback: str = "und") -> str:
    """ISO 639-2 (por/eng/spa/...) pelo nome do arquivo, depois pelas pastas
    imediatas (Subs/pt-BR/...), depois pelo conteúdo; senão `fallback`."""
    path = Path(path)
    lang = _lang_from_tokens(_tokens(path, video_path))
    if lang is None:
        for d in list(path.parents)[:2]:
            # pasta com referência de episódio ou nome de release não é pista
            if parse.parse_episode_refs(d.name) or len(d.name) > 24:
                continue
            lang = _lang_from_tokens(_split(d.name))
            if lang:
                break
    return lang or guess_language_from_text(path) or fallback


def guess_language_from_text(path) -> str | None:
    try:
        text = _read_text(Path(path))
    except OSError:
        return None
    words = re.findall(r"[a-záàâãéêíóôõúçñ']+", text.lower())
    if len(words) < 30:
        return None
    scores = {lang: sum(1 for w in words if w in sw)
              for lang, sw in _STOPWORDS.items()}
    best = max(scores, key=scores.get)
    ranked = sorted(scores.values(), reverse=True)
    # precisa de sinal claro: vencedor com folga sobre o segundo
    if ranked[0] >= 8 and ranked[0] >= 1.5 * max(1, ranked[1]):
        return best
    return None


def flavor_of(path, video_path: str | None = None) -> str:
    """'forced' | 'sdh' | 'normal' pelo nome."""
    toks = set(_tokens(Path(path), video_path))
    if toks & {"forced", "forçada", "forcada"}:
        return "forced"
    if toks & {"sdh", "hi", "cc", "hearing"}:
        return "sdh"
    return "normal"


# -------------------- ler / escrever --------------------

def _read_text(path: Path) -> str:
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "utf-8"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            pass
    return raw.decode("cp1252", errors="replace")


def _parse_srt(text: str) -> list[tuple[float, float, str]]:
    cues: list[tuple[float, float, str]] = []
    blocks = re.split(r"\r?\n\s*\r?\n", text.strip())
    for blk in blocks:
        lines = blk.strip().splitlines()
        if not lines:
            continue
        m = None
        for i, ln in enumerate(lines[:2]):
            m = _TS_RE.search(ln)
            if m:
                body = lines[i + 1:]
                break
        if not m:
            continue
        g = [int(x) for x in m.groups()]
        st = g[0] * 3600 + g[1] * 60 + g[2] + g[3] / (1000 if len(m.group(4)) == 3 else 10 ** len(m.group(4)))
        en = g[4] * 3600 + g[5] * 60 + g[6] + g[7] / (1000 if len(m.group(8)) == 3 else 10 ** len(m.group(8)))
        txt = "\n".join(body).strip()
        if txt and en > st:
            cues.append((st, en, txt))
    return cues


def load_cues(path) -> list[tuple[float, float, str]]:
    """Cues (início, fim, texto) em segundos, de srt/ass/ssa/vtt."""
    path = Path(path)
    text = _read_text(path)
    if path.suffix.lower() == ".srt":
        return _parse_srt(text)
    # ass/ssa/vtt: o ffmpeg converte para SRT (a partir do texto já em UTF-8)
    with tempfile.TemporaryDirectory(prefix="sub_conv_") as td:
        src = Path(td) / ("in" + path.suffix.lower())
        src.write_text(text, encoding="utf-8")
        p = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-i", str(src), "-f", "srt", "-"],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
    if p.returncode != 0:
        raise merger.MergeError(
            f"conversão de legenda falhou ({path.name}): {p.stderr.strip()[-300:]}")
    return _parse_srt(p.stdout)


def _fmt(t: float) -> str:
    ms = int(round(max(0.0, t) * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_srt(cues, path) -> None:
    lines = []
    for i, (st, en, txt) in enumerate(cues, start=1):
        lines += [str(i), f"{_fmt(st)} --> {_fmt(en)}", txt, ""]
    Path(path).write_text("\n".join(lines), encoding="utf-8")


# -------------------- remapear --------------------

def remap(cues, fn) -> list[tuple[float, float, str]]:
    """fn(t) -> t' (ou None = trecho descartado). Cue cujo início cai fora
    some; fim fora mantém a duração original."""
    out = []
    for st, en, txt in cues:
        s2 = fn(st)
        if s2 is None or s2 < 0:
            continue
        e2 = fn(en)
        if e2 is None or e2 <= s2:
            e2 = s2 + (en - st)
        out.append((s2, e2, txt))
    return out


def shift_fn(delta: float):
    return lambda t: t + delta


def edl_fn(segments: list[dict], b_shift: float = 0.0):
    """Tempo do lado DUBLADO (a) → timeline de saída (b − b_shift), pela EDL.

    match/drift: b = a + offset; pal: proporcional; replaced com use_dub:
    proporcional; gap_dub / replaced descartada / fora de segmento: None."""
    segs = sorted((s for s in segments if s.get("a_end") is not None
                   and s.get("a_start") is not None), key=lambda s: s["a_start"])

    def fn(t: float):
        for s in segs:
            if not (s["a_start"] <= t < s["a_end"]):
                continue
            kind = s.get("kind")
            if kind in ("match", "drift") and s.get("offset") is not None:
                return t + s["offset"] - b_shift
            if kind == "pal" or (kind == "replaced" and s.get("action") == "use_dub"):
                a_len = s["a_end"] - s["a_start"]
                b0, b1 = s.get("b_start"), s.get("b_end")
                if b0 is None or b1 is None or a_len <= 0:
                    return None
                return b0 + (t - s["a_start"]) * (b1 - b0) / a_len - b_shift
            return None
        return None
    return fn


# -------------------- planejar / muxar --------------------

def embedded_text_keys(probe: dict) -> set[tuple[str, str]]:
    """(idioma, 'forced'|'normal') das legendas de TEXTO já no arquivo."""
    keys = set()
    for s in merger.get_streams(probe, "subtitle"):
        if (s.get("codec_name") or "").lower() not in TEXT_CODECS:
            continue
        lang = merger.canonical_lang(merger.raw_lang_of(s))
        keys.add((lang, "forced" if merger.is_forced(s) else "normal"))
    return keys


def plan(orig_subs: list, dub_subs: list, embedded: set[tuple[str, str]],
         orig_video: str | None = None, dub_video: str | None = None,
         orig_lang: str = "und", dub_lang: str = "und") -> tuple[list[dict], list[str]]:
    """Decide quais legendas externas entram. Retorna (itens, motivos de
    descarte). Item: {path, side, lang, flavor}."""
    items: list[dict] = []
    skipped: list[str] = []
    seen: set[tuple[str, str]] = set()
    for side, subs, video, fb in (("orig", orig_subs, orig_video, orig_lang),
                                  ("dub", dub_subs, dub_video, dub_lang)):
        for sp in subs:
            sp = Path(sp)
            lang = guess_language(sp, video, fallback=fb)
            flavor = flavor_of(sp, video)
            key = (lang, "forced" if flavor == "forced" else "normal")
            if key in embedded:
                skipped.append(f"{sp.name}: já existe legenda {lang}"
                               f"{' forçada' if flavor == 'forced' else ''} embutida")
                continue
            if key in seen and flavor != "sdh":
                skipped.append(f"{sp.name}: duplicada ({lang}) — mantida a do "
                               f"{'original' if side == 'dub' else 'mesmo lado'}")
                continue
            if flavor == "sdh" and (lang, "sdh") in seen:
                skipped.append(f"{sp.name}: duplicada ({lang} SDH)")
                continue
            seen.add((lang, "sdh") if flavor == "sdh" else key)
            items.append({"path": str(sp), "side": side, "lang": lang,
                          "flavor": flavor})
    return items, skipped


def mux(output: str, items: list[dict], log=print) -> int:
    """Anexa `items` (cada um com `srt` já remapeado, `lang`, `flavor`) ao MKV
    `output`, reescrevendo-o em cópia. Retorna quantas faixas entraram."""
    items = [it for it in items if it.get("srt") and Path(it["srt"]).exists()
             and Path(it["srt"]).stat().st_size > 0]
    if not items:
        return 0
    out = Path(output)
    probe = merger.ffprobe_json(str(out))
    n_sub = len(merger.get_streams(probe, "subtitle"))
    tmp = out.with_name(out.stem + ".subs_tmp" + out.suffix)
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(out)]
    for it in items:
        cmd += ["-i", it["srt"]]
    cmd += ["-map", "0", "-c", "copy", "-map_chapters", "0", "-map_metadata", "0"]
    for k, it in enumerate(items):
        idx = n_sub + k
        cmd += ["-map", f"{k + 1}:0", f"-c:s:{idx}", "srt",
                f"-metadata:s:s:{idx}", f"language={it['lang']}"]
        title = {"forced": "Forçada", "sdh": "SDH"}.get(it["flavor"], "")
        cmd += [f"-metadata:s:s:{idx}", f"title={title}",
                f"-disposition:s:{idx}", "forced" if it["flavor"] == "forced" else "0"]
    cmd += ["-avoid_negative_ts", "make_zero", "-max_interleave_delta", "0", str(tmp)]
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    if p.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise merger.MergeError(f"mux de legendas falhou: {p.stderr.strip()[-600:]}")
    os.replace(tmp, out)
    log("Legendas externas anexadas: " + ", ".join(
        f"{Path(it['path']).name} → {it['lang']}"
        + (f" ({it['flavor']})" if it['flavor'] != 'normal' else "")
        for it in items))
    return len(items)


def sidecar(output: str, items: list[dict], log=print) -> int:
    """Alternativa ao mux: grava as legendas AO LADO do arquivo entregue
    (`Filme.por.srt`, `Filme.por.forced.srt`) — usado quando a saída é um
    hardlink do torrent (muxar obrigaria a copiar o filme inteiro) ou não é
    MKV. Jellyfin/Plex leem sidecars normalmente."""
    out = Path(output)
    n = 0
    for it in items:
        if not it.get("srt"):
            continue
        suffix = {"forced": ".forced", "sdh": ".sdh"}.get(it["flavor"], "")
        dst = out.with_name(f"{out.stem}.{it['lang']}{suffix}.srt")
        k = 2
        while dst.exists():
            dst = out.with_name(f"{out.stem}.{it['lang']}{suffix}.{k}.srt")
            k += 1
        dst.write_bytes(Path(it["srt"]).read_bytes())  # o srt está num temp dir
        n += 1
        log(f"Legenda externa gravada ao lado: {dst.name} (de {Path(it['path']).name})")
    return n


def attach(output: str, orig_subs: list, dub_subs: list, orig_fn, dub_fn,
           orig_video: str | None = None, dub_video: str | None = None,
           orig_lang: str = "und", dub_lang: str = "und", log=print,
           mode: str = "mux") -> int:
    """Ponta a ponta: planeja, remapeia (orig_fn/dub_fn: tempo→tempo ou
    None para 'lado indisponível') e muxa (mode='mux') ou grava sidecars
    (mode='sidecar'). Falha de UMA legenda só a pula."""
    if not orig_subs and not dub_subs:
        return 0
    probe = merger.ffprobe_json(output)
    items, skipped = plan(orig_subs, dub_subs, embedded_text_keys(probe),
                          orig_video, dub_video, orig_lang, dub_lang)
    for s in skipped:
        log(f"legenda ignorada — {s}")
    ready = []
    with tempfile.TemporaryDirectory(prefix="subs_mux_") as td:
        for i, it in enumerate(items):
            fn = orig_fn if it["side"] == "orig" else dub_fn
            if fn is None:
                log(f"legenda ignorada — {Path(it['path']).name}: sem referência "
                    f"de tempo para o lado {it['side']}")
                continue
            try:
                cues = remap(load_cues(it["path"]), fn)
            except Exception as e:  # noqa: BLE001 — uma legenda ruim não derruba o episódio
                log(f"⚠️ legenda {Path(it['path']).name} ignorada: {e}")
                continue
            if not cues:
                log(f"legenda ignorada — {Path(it['path']).name}: nenhuma cue "
                    f"sobrou depois do remapeamento")
                continue
            srt = Path(td) / f"sub{i}.srt"
            write_srt(cues, srt)
            ready.append({**it, "srt": str(srt)})
        if mode == "sidecar":
            return sidecar(output, ready, log)
        return mux(output, ready, log)
