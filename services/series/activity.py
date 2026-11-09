"""Episódios de uma série em jobs ATIVOS — o "baixando agora" do modal.

O modal de série mostra o que já está na coleção (scan dos destinos, em
services/catalog) e, por cima, o que um job em andamento ainda vai trazer.
Pedir de novo um episódio que outro job está baixando cria um segundo job
escrevendo o MESMO arquivo de saída — e o cancelamento de um apaga a entrega
do outro (caso real, com dois jobs duplicados da mesma série).

Lê só `jobs._jobs` (memória): jobs terminais já saíram dali, e o
invalidate_library do fim do job faz o scan enxergar o que eles entregaram.
Os episódios que um job AINDA ATIVO já concluiu também voltam (`done`): estão
no destino, mas o cache do scan (TTL de 30 min) só é invalidado quando o job
inteiro termina.
"""
from services import jobs
from services.jobs import runtime

# a caminho do destino: o mesmo _IN_FLIGHT do relatório (merge_runner) mais
# `review` — parado esperando a revisão de alinhamento, mas ainda deste job
# (resolver a revisão é o caminho, não pedir de novo)
IN_FLIGHT = ("pending", "downloading", "downloaded", "aligning", "merging",
             "review")


def episodes_in_jobs(tmdb_id: int) -> dict:
    """{seasons: {temporada: [{episode, state, job_id, job_status, kind}]},
    done: {temporada: [episódios]}} da série nos jobs ativos.

    Recompressão fica de fora: reconverte arquivos que JÁ estão na coleção,
    não traz episódio novo (e pedir o download de novo não colide com ela).
    Dois jobs ativos com o mesmo episódio: vale o mais antigo (é o que vai
    entregar primeiro e o que o usuário provavelmente quer acompanhar)."""
    active: dict[int, dict[int, dict]] = {}
    done: dict[int, set[int]] = {}
    ativos = sorted(jobs._jobs.values(), key=lambda j: j.get("created_at") or "")
    for job in ativos:
        if job.get("media_type") != "tv" or job.get("tmdb_id") != tmdb_id:
            continue
        if runtime._is_recompress(job):
            continue
        for ep in (job.get("episodes") or {}).values():
            season, episode = ep.get("season"), ep.get("episode")
            if season is None or episode is None:
                continue
            state = ep.get("state")
            if state == "done":
                done.setdefault(season, set()).add(episode)
            elif state in IN_FLIGHT:
                active.setdefault(season, {}).setdefault(episode, {
                    "episode": episode,
                    "state": state,
                    "job_id": job["id"],
                    "job_status": job.get("status"),
                    "kind": job.get("kind"),
                })
    # concluído num job e a caminho noutro continua nos dois: o segundo job
    # vai reescrever o mesmo arquivo, e pedir um terceiro colidiria com ele
    return {
        "seasons": {s: [v for _e, v in sorted(eps.items())]
                    for s, eps in sorted(active.items())},
        "done": {s: sorted(eps) for s, eps in sorted(done.items())},
    }
