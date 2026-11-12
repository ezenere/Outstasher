"""Ficha do episódio (e do filme): tudo o que se sabe dele num lugar só.

- arquivos: origem do original e do dublado, saída, legendas externas;
- de onde veio cada lado: o torrent do plano que cobre o episódio;
- processamento: estado, erro, método (hardlink, offset, drift, EDL), offset
  e janelas medidas, o plano do render (fatias e cortes com o motivo) e a
  EDL do alinhador;
- o log só daquele episódio;
- faixas de cada arquivo (ffprobe), numa rota à parte: sondar arquivos de
  dezenas de GB num disco lento não pode segurar a abertura da ficha.
"""
import json
import subprocess
from pathlib import Path

from services import store
from services.jobs.runtime import _lookup

PROBE_TIMEOUT_S = 25


def episode(job_id: str, key: str) -> dict | None:
    job = _lookup(job_id)
    if not job or job.get("media_type") != "tv":
        return None
    ep = (job.get("episodes") or {}).get(key)
    if ep is None:
        return None
    src = ep.get("src") or {}
    return {
        "key": key,
        "season": ep.get("season"), "episode": ep.get("episode"),
        "name": ep.get("name"), "air_date": ep.get("air_date"),
        "runtime": ep.get("runtime"),
        "state": ep.get("state"), "error": ep.get("error"),
        # src.file: job de um lado só (só original / só dublado)
        "files": {"original": src.get("original"), "dubbed": src.get("dubbed"),
                  "file": src.get("file"), "output": ep.get("output")},
        "subs": ep.get("subs") or {},
        "torrents": _torrents_for(job, key),
        "edl": ep.get("edl"),
        "report": store.load_report(job_id, key),
        "events": store.load_episode_events(job_id, key),
    }


def movie(job_id: str) -> dict | None:
    job = _lookup(job_id)
    if not job or job.get("media_type") == "tv":
        return None
    rep = store.load_report(job_id, store.REPORT_MOVIE_KEY) or {}
    adv = job.get("advanced") or {}
    drift = job.get("drift_confirm") or {}
    srcs = rep.get("sources") or {}
    return {
        "key": None,
        "state": job.get("status"), "error": job.get("detail")
        if job.get("status") == "error" else None,
        "files": {"original": srcs.get("original") or adv.get("video_file")
                  or drift.get("video_file") or job.get("manual_video"),
                  "dubbed": srcs.get("dubbed") or adv.get("audio_file")
                  or drift.get("audio_file") or job.get("manual_audio"),
                  "output": job.get("output")},
        "subs": {},
        "torrents": [],
        "edl": adv.get("edl"),
        "report": rep or None,
        "events": [],
    }


def _torrents_for(job: dict, key: str) -> list[dict]:
    """Torrents do plano que trazem o episódio, com o papel de cada um."""
    out = []
    for t in job.get("torrents") or []:
        if key not in (t.get("coverage") or []):
            continue
        out.append({"n": t.get("n"), "role": t.get("role"),
                    "title": t.get("title"), "tracker": t.get("tracker"),
                    "state": t.get("state"), "size": t.get("size"),
                    "content_path": t.get("content_path")})
    return out


# -------------------- faixas (ffprobe) --------------------

def probe_paths(paths: dict[str, str | None]) -> dict[str, dict]:
    """{papel: resumo do arquivo} — existe? tamanho, duração, faixas."""
    return {role: _probe_one(p) for role, p in paths.items() if p}


def _probe_one(path: str) -> dict:
    p = Path(path)
    out: dict = {"path": path, "exists": False}
    try:
        st = p.stat()
    except OSError:
        return out
    out["exists"] = True
    out["size"] = st.st_size
    if p.is_dir():
        out["error"] = "é uma pasta"
        return out
    try:
        r = subprocess.run(
            ["ffprobe", "-hide_banner", "-loglevel", "error", "-print_format",
             "json", "-show_format", "-show_streams", "-show_chapters", path],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=PROBE_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        out["error"] = f"ffprobe não respondeu em {PROBE_TIMEOUT_S}s"
        return out
    if r.returncode != 0:
        out["error"] = (r.stderr.strip() or "ffprobe falhou")[-300:]
        return out
    data = json.loads(r.stdout or "{}")
    fmt = data.get("format") or {}
    out["duration"] = _f(fmt.get("duration"))
    out["bit_rate"] = _i(fmt.get("bit_rate"))
    out["format"] = fmt.get("format_long_name") or fmt.get("format_name")
    out["chapters"] = len(data.get("chapters") or [])
    out["streams"] = [_stream(s) for s in data.get("streams") or []]
    return out


def _stream(s: dict) -> dict:
    tags = {k.lower(): v for k, v in (s.get("tags") or {}).items()}
    disp = s.get("disposition") or {}
    item = {
        "index": s.get("index"), "type": s.get("codec_type"),
        "codec": s.get("codec_name"), "profile": s.get("profile"),
        "lang": tags.get("language"), "title": tags.get("title"),
        "default": bool(disp.get("default")), "forced": bool(disp.get("forced")),
        "cover": bool(disp.get("attached_pic")),
        "bit_rate": _i(s.get("bit_rate") or tags.get("bps")),
    }
    if s.get("codec_type") == "video":
        item.update(width=s.get("width"), height=s.get("height"),
                    fps=_rate(s.get("avg_frame_rate") or s.get("r_frame_rate")),
                    pix_fmt=s.get("pix_fmt"),
                    hdr=s.get("color_transfer") in ("smpte2084", "arib-std-b67"))
    elif s.get("codec_type") == "audio":
        item.update(channels=s.get("channels"), layout=s.get("channel_layout"),
                    sample_rate=_i(s.get("sample_rate")))
    return item


def _f(v) -> float | None:
    try:
        return round(float(v), 3)
    except (TypeError, ValueError):
        return None


def _i(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _rate(v) -> float | None:
    try:
        n, d = str(v).split("/")
        return round(int(n) / int(d), 3) if int(d) else None
    except (TypeError, ValueError):
        return None
