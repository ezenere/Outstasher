"""Rebusca de correspondências entre episódios DESALINHADOS.

Quando o alinhamento por conteúdo conclui que um par não tem quase nada em
comum ("conflito de alinhamento"), o sintoma clássico é ordem de episódios
trocada entre as duas versões: cada arquivo é de um episódio real, só que
casado com o parceiro errado. Com 2+ episódios nessa situação dá para cruzar
todos × todos — o fingerprint de cada arquivo já está pago (cache em disco) e
o localizador grosseiro (`coarse_offset`, voto por diagonal) diz em segundos
se o conteúdo de um dublado está dentro de um original.

O fluxo é um gate: o job pausa listando os desalinhados e o usuário escolhe
- tentar casar AUTOMATICAMENTE (todos × todos, melhor fração ganha),
- casar MANUALMENTE (dropdown de dublado por original), ou
- FINALIZAR o processamento sem eles.
Nos dois primeiros dá para IGNORAR episódios (ficam fora da conta dos 75%,
como qualquer pulo decidido pelo usuário). Enquanto sobrarem 2+ desalinhados
o gate reabre — inclusive depois de uma tentativa automática.

A identidade do episódio é do lado ORIGINAL: é dele o vídeo entregue e a
numeração da estante. O rematch só decide QUAL áudio dublado pertence a cada
original.
"""
import asyncio
from pathlib import Path

from services import jobs
from services.series.align import fingerprint

MISMATCH_PREFIX = "conflito de alinhamento"
# fração mínima de frames do dublado votando na mesma diagonal do original
# para o casamento automático valer (o alinhamento completo ainda valida)
MIN_FRACTION = 0.30


def mismatched(job: dict) -> dict:
    """Episódios que falharam por conflito de alinhamento (o pool do rematch)."""
    return {k: ep for k, ep in job["episodes"].items()
            if ep.get("state") == "failed"
            and str(ep.get("error") or "").startswith(MISMATCH_PREFIX)}


def gate_payload(job: dict) -> dict:
    eps = {}
    for k, ep in sorted(mismatched(job).items()):
        src = ep.get("src") or {}
        eps[k] = {
            "original": Path(src.get("original") or "?").name,
            "dubbed": Path(src.get("dubbed") or "?").name,
            "error": str(ep.get("error") or ""),
        }
    return {"mismatched": eps, "attempts": int(job.get("rematch_attempts") or 0)}


def score_all(pool: dict, log=print, on_progress=None) -> list[tuple]:
    """Placar todos × todos: [(frac, offset_s, orig_key, dub_key), ...].

    Cada lado entra pelo ARQUIVO (fingerprint cacheado); o placar de um par é
    a fração de frames do dublado que votam numa mesma diagonal do original —
    conteúdo igual concentra os votos, conteúdo sem relação espalha.
    """
    keys = sorted(pool)
    prints: dict[tuple[str, str], object] = {}   # (lado, key) -> hashes

    def fp(side: str, key: str):
        if (side, key) not in prints:
            path = pool[key]["src"][side]
            if on_progress:
                on_progress(side, key)
            crop = fingerprint.crop_params(path)
            prints[(side, key)] = fingerprint.dhash_cached(path, crop)
        return prints[(side, key)]

    scores = []
    for ok in keys:
        hb = fp("original", ok)
        for dk in keys:
            ha = fp("dubbed", dk)
            short, long_ = (ha, hb) if len(ha) <= len(hb) else (hb, ha)
            off, frac = fingerprint.coarse_offset(short, long_)
            scores.append((float(frac), off / fingerprint.FPS, ok, dk))
            log(f"  {dk} (dub) × {ok} (orig): {frac:.0%}")
    scores.sort(key=lambda s: -s[0])
    return scores


def assign(scores: list[tuple], min_frac: float = MIN_FRACTION) -> dict:
    """Guloso no placar: melhor fração primeiro, cada lado casa uma vez.
    Retorna {orig_key: (dub_key, frac)} só com casamentos acima do piso."""
    pairs: dict[str, tuple[str, float]] = {}
    used_dub: set[str] = set()
    for frac, _off, ok, dk in scores:
        if frac < min_frac:
            break
        if ok in pairs or dk in used_dub:
            continue
        pairs[ok] = (dk, frac)
        used_dub.add(dk)
    return pairs


def apply_ignores(job: dict, keys: list[str]):
    """Ignorados viram pulo decidido pelo usuário: fora da conta dos 75%."""
    pool = mismatched(job)
    for k in keys:
        if k in pool:
            ep = job["episodes"][k]
            ep["state"] = "skipped_mismatch"
            ep["error"] = None
            jobs._event(job, "chosen", f"{k}: ignorado no rematch (decisão do usuário)")


def apply_pairs(job: dict, pairs: dict[str, str], fracs: dict[str, float] | None = None):
    """Religa cada original ao dublado escolhido e devolve os episódios à fila
    de merge (state=downloaded). O dublado vem de OUTRO episódio do pool — o
    arquivo, não o episódio: quem manda na identidade é o original."""
    pool = mismatched(job)
    for ok, dk in pairs.items():
        if ok not in pool or dk not in pool:
            raise ValueError(f"par inválido: {ok} <- {dk} (fora do pool)")
    # snapshot ANTES de mexer: numa troca A<->B, o segundo par leria o
    # caminho já sobrescrito pelo primeiro
    dub_de = {k: job["episodes"][k]["src"]["dubbed"] for k in pool}
    for ok, dk in pairs.items():
        ep = job["episodes"][ok]
        ep["src"]["dubbed"] = dub_de[dk]
        ep["state"] = "downloaded"
        ep["error"] = None
        ep.pop("edl", None)
        ep["output"] = None
        pct = f" ({fracs[ok]:.0%} dos frames)" if fracs and ok in fracs else ""
        jobs._event(job, "chosen",
                    f"{ok}: áudio dublado religado ao arquivo de {dk}{pct}")


async def run_auto(job: dict, ignore: list[str]):
    """Continuação do gate: todos × todos, aplica os casamentos e volta ao
    merge. Ao final do merge o gate reabre sozinho se sobrarem 2+."""
    from services.series import merge_runner
    try:
        apply_ignores(job, ignore)
        pool = mismatched(job)
        job["rematch_attempts"] = int(job.get("rematch_attempts") or 0) + 1
        jobs._set(job, "merging",
                  f"Cruzando {len(pool)} episódio(s) desalinhado(s)...")

        def on_fp(side, key):
            papel = "Áudio" if side == "dubbed" else "Vídeo"
            job["detail"] = f"{key}: fingerprint ({papel}) para o rematch..."

        log = lambda m: jobs._event(job, "merge", m)  # noqa: E731
        scores = await asyncio.to_thread(score_all, pool, log, on_fp)
        pairs = assign(scores)
        if pairs:
            fracs = {ok: frac for ok, (dk, frac) in pairs.items()}
            apply_pairs(job, {ok: dk for ok, (dk, frac) in pairs.items()}, fracs)
            jobs._event(job, "merge",
                        f"Rematch: {len(pairs)} correspondência(s) encontrada(s), "
                        f"{len(pool) - len(pairs)} sem par")
        else:
            jobs._event(job, "merge",
                        "Rematch: nenhuma correspondência acima do piso "
                        f"({MIN_FRACTION:.0%}) — nada casou")
        await merge_runner.merge_all(job)
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001
        jobs._fail(job, f"{type(e).__name__}: {e}")


async def finish_now(job: dict):
    """Decisão "finalizar processamento": fecha o relatório com o que há."""
    from services.series import merge_runner
    try:
        job["_rematch_done"] = True
        jobs._event(job, "chosen",
                    "Usuário finalizou o processamento sem casar os "
                    "desalinhados restantes")
        merge_runner._finish(job)
        await merge_runner._cleanup_torrents(job)
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001
        jobs._fail(job, f"{type(e).__name__}: {e}")
