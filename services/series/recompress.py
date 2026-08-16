"""Recompressão em LOTE (série inteira, temporada ou episódios escolhidos).

Um job media_type=tv com um item por arquivo: cada um passa pelo mesmo fluxo
da recompressão de filme (tmp na mesma pasta, descarta se ficou maior,
replace atômico), sob o lock global POR ITEM. Falha de um item não para os
outros; a regra dos 75% e o relatório são os mesmos do merge de séries.

Validação de integridade (lição do DECODE_QSV.md): decoder/encoder de
hardware pode DESCARTAR frames silenciosamente com exit code 0. Antes de
substituir o original, a contagem de PACOTES de vídeo do resultado precisa
bater com a da origem (>= 99,5%) — pacotes, não frames decodificados: a
checagem custa uma leitura de container, não um decode.
"""
import asyncio
import subprocess
import uuid
from datetime import datetime
from pathlib import Path

from services import catalog, store, tmdb, transcode
from services import jobs
from services.series import merge_runner

MIN_PACKET_RATIO = 0.995


async def create_recompress_batch(destination_id: int | None, folder: str,
                                  rels: list[str], convert: dict,
                                  replace: bool = True,
                                  tmdb_id: int | None = None) -> dict:
    """Cria o job de recompressão em lote. `rels` são caminhos relativos à
    pasta da série (a UI manda a temporada/série expandida em arquivos)."""
    if not rels:
        raise ValueError("Nenhum arquivo selecionado")

    def _prepare():
        opts = transcode.validate(convert)
        if opts.is_noop():
            raise ValueError("As opções escolhidas não mudam nada no arquivo")
        srcs = []
        for rel in rels:
            src = catalog.media_path(destination_id, folder, rel)
            srcs.append((rel, src))
        dest = (store.get_destination(destination_id) if destination_id
                else store.default_destination())
        if dest is None:
            raise ValueError("Nenhum destino cadastrado")
        return opts, srcs, dest

    opts, srcs, dest = await asyncio.to_thread(_prepare)

    job = {
        "id": uuid.uuid4().hex[:10],
        "media_type": "tv",
        "tmdb_id": tmdb_id or catalog.tmdb_id_in(folder),
        "language": "pt",  # só para os metadados do TMDB (capa/descrição)
        "mode": "recompress",
        "kind": None,
        "convert": opts.to_dict(),
        "status": "merging",
        "detail": "Preparando recompressão em lote...",
        "movie": None,
        "request": None,
        "episodes": {},
        "torrents": [],
        "awaiting": None,
        "report": None,
        "progress": {},
        "output": None,
        "destination_id": dest["id"],
        "destination_label": dest["label"],
        "destination_path": dest["path"],
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "recompress": {"folder": folder, "rels": rels,
                       "replace": bool(replace)},
    }
    for rel, src in srcs:
        ref = catalog.parse_episode_filename(Path(rel).name)
        key = f"S{ref[0]:02d}E{ref[1]:02d}" if ref else Path(rel).stem
        job["episodes"][key] = {
            "season": ref[0] if ref else 0,
            "episode": ref[1] if ref else 0,
            "name": Path(rel).name, "air_date": None, "runtime": None,
            "state": "downloaded",  # "baixado" = pronto para converter
            "src": {"file": str(src)}, "rel": rel,
            "output": None, "error": None,
        }
    jobs._jobs[job["id"]] = job
    conv = transcode.describe(opts)
    jobs._event(job, "status",
                f"Recompressão em lote criada ({len(rels)} arquivo(s), "
                f"{'substituindo' if replace else 'mantendo'} os originais) — "
                f"{folder}" + (f" — conversão: {', '.join(conv)}" if conv else ""))
    jobs._spawn(job["id"], _run(job))
    return jobs._public(job)


async def _run(job: dict):
    try:
        if job.get("tmdb_id"):
            try:
                job["movie"] = await tmdb.tv_details(job["tmdb_id"])
                job["movie"]["original_title"] = job["movie"].get("original_title")
            except Exception:  # noqa: BLE001 — metadado é cosmético aqui
                pass
        opts = transcode.validate(job["convert"])
        keys = sorted(k for k, v in job["episodes"].items()
                      if v["state"] == "downloaded")
        for i, key in enumerate(keys, start=1):
            ep = job["episodes"][key]
            lock = jobs._get_merge_lock()
            if lock.locked():
                job["detail"] = f"{key}: na fila de conversão ({i}/{len(keys)})..."
            async with lock:
                ep["state"] = "merging"
                job["detail"] = f"{key}: recomprimindo ({i}/{len(keys)})..."
                try:
                    await _recompress_one(job, ep, opts)
                    ep["state"] = "done"
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001 — falha por item
                    if job["id"] in jobs._cancelling:
                        raise asyncio.CancelledError
                    ep["state"] = "failed"
                    ep["error"] = f"{type(e).__name__}: {e}"
                    jobs._event(job, "merge", f"❌ {key}: {ep['error']}")
                finally:
                    job["progress"]["merge"] = None
                    job["output"] = None
            store.upsert_job(job)
        merge_runner._finish(job)
        catalog.invalidate_library()
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001
        jobs._fail(job, f"{type(e).__name__}: {e}")


def _packet_count(path: str) -> int:
    """Pacotes de vídeo do container (leitura, sem decode)."""
    p = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-count_packets", "-show_entries", "stream=nb_read_packets",
         "-of", "csv=p=0", path],
        capture_output=True, text=True, timeout=600)
    try:
        return int(p.stdout.strip().splitlines()[0])
    except (ValueError, IndexError):
        return 0


async def _recompress_one(job: dict, ep: dict, opts):
    src = Path(ep["src"]["file"])
    if not src.is_file():
        raise RuntimeError(f"arquivo sumiu da coleção: {src}")
    before = src.stat().st_size
    tmp = src.with_name(f".{src.stem}.recompress-{job['id']}.mkv")
    job["output"] = str(tmp)  # cancel() apaga o tmp, nunca o original
    job["merge_started_at"] = datetime.now().isoformat(timespec="seconds")

    log, on_progress = jobs._ffmpeg_hooks(job)
    try:
        result = await asyncio.to_thread(
            transcode.convert_single, str(src), str(tmp), opts,
            None, (job.get("movie") or {}).get("original_language"),
            log=log, on_progress=on_progress,
            on_start=jobs._register_proc(job["id"]))
    finally:
        jobs._ffmpeg_procs.pop(job["id"], None)

    out = Path(result.output)
    if result.linked:
        await asyncio.to_thread(out.unlink, True)
        ep["output"] = str(src)
        jobs._event(job, "merge",
                    f"{ep['name']}: nada a recomprimir — mantido")
        return

    # integridade ANTES de substituir: encoder de hardware pode ter descartado
    # frames em silêncio (incidente DECODE_QSV) — pacotes têm que bater
    src_pkts, out_pkts = await asyncio.gather(
        asyncio.to_thread(_packet_count, str(src)),
        asyncio.to_thread(_packet_count, str(out)))
    if src_pkts > 0 and out_pkts < src_pkts * MIN_PACKET_RATIO:
        await asyncio.to_thread(out.unlink, True)
        raise RuntimeError(
            f"resultado com {out_pkts} pacotes de vídeo vs {src_pkts} da "
            f"origem ({out_pkts * 100 // max(1, src_pkts)}%) — encoder "
            f"descartou frames; original mantido")

    def _finalize() -> str | None:
        after = out.stat().st_size
        if after >= before:
            out.unlink(missing_ok=True)
            return None
        if job["recompress"]["replace"]:
            out.replace(src)
            return str(src)
        final = src.with_name(f"{src.stem} [recomprimido].mkv")
        n = 2
        while final.exists():
            final = src.with_name(f"{src.stem} [recomprimido] ({n}).mkv")
            n += 1
        out.replace(final)
        return str(final)

    final_path = await asyncio.to_thread(_finalize)
    if final_path is None:
        ep["output"] = str(src)
        jobs._event(job, "merge",
                    f"{ep['name']}: resultado ficou maior — original mantido")
        return
    ep["output"] = final_path
    after = Path(final_path).stat().st_size
    jobs._event(job, "merge",
                f"✅ {ep['name']}: {catalog.human_size(before)} → "
                f"{catalog.human_size(after)}")
