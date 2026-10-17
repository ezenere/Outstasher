"""Migração única do banco para o schema normalizado.

Até aqui cada job era UM documento JSON em `jobs.data`, com as listas dentro
(candidatos, episódios, torrents, EDLs...). O código novo já LÊ esse formato
(cada seção tem a sua normalização legada no store) e GRAVA nas tabelas — então
migrar é ler cada job pelo caminho normal e regravá-lo pelo caminho normal.
Nada de transformação paralela aqui: se o store sabe ler e gravar, a migração
está certa por construção.

Ordem por created_at CRESCENTE: os metadados do TMDB (media*) são
compartilhados entre jobs e a última gravação vence — um job antigo regravado
depois de um mais novo deixaria título/pôster/temporadas defasados.

Cada job é uma transação própria: se um falhar, ele continua no formato antigo
(que o código segue lendo) e os outros seguem migrados. O relatório lista as
falhas.

Uso (no container):  python -m services.migrate_db [--sem-backup]
"""
import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime

import config


def _tamanho_data(conn) -> int:
    return conn.execute("SELECT IFNULL(SUM(LENGTH(data)), 0) FROM jobs").fetchone()[0]


def _backup(caminho) -> str:
    destino = caminho.with_name(
        f"{caminho.stem}.pre-normalizacao-{datetime.now():%Y%m%d-%H%M%S}{caminho.suffix}")
    src = sqlite3.connect(f"file:{caminho}?mode=ro", uri=True)
    dst = sqlite3.connect(destino)
    src.backup(dst)
    dst.close()
    src.close()
    return str(destino)


def migrar(fazer_backup: bool = True, log=print) -> dict:
    from services import store

    db = config.DB_FILE
    if not db.exists():
        raise SystemExit(f"Banco não encontrado: {db}")
    tamanho_arquivo = db.stat().st_size
    if fazer_backup:
        log(f"Backup: {_backup(db)}")

    # init cria tabelas/colunas novas e migra o que é de configuração
    store.init()
    conn = store._conn
    antes = _tamanho_data(conn)
    ids = [r[0] for r in conn.execute("SELECT id FROM jobs ORDER BY created_at, id")]
    log(f"{len(ids)} job(s) a regravar")

    falhas: list[str] = []
    eventos = 0
    t0 = time.monotonic()
    for i, job_id in enumerate(ids, 1):
        try:
            job = store.get_job(job_id)          # normaliza o formato antigo
            if job is None:
                continue
            store.upsert_job(job)                # grava nas tabelas
            eventos += store.migrate_candidate_events(job_id)
        except Exception as e:  # noqa: BLE001 — um job não derruba a migração
            falhas.append(f"{job_id}: {type(e).__name__}: {e}")
            log(f"  ⚠️ {job_id}: {type(e).__name__}: {e}")
        if i % 25 == 0:
            log(f"  {i}/{len(ids)}...")

    depois = _tamanho_data(conn)
    # o que sobrou no documento: deve ser só configuração (convert,
    # advanced_merge, review_rules) e chaves desconhecidas guardadas sem perda
    sobras: dict[str, int] = {}
    for (data,) in conn.execute("SELECT data FROM jobs"):
        for k in json.loads(data or "{}"):
            sobras[k] = sobras.get(k, 0) + 1

    conn.commit()
    log("VACUUM...")
    conn.execute("VACUUM")
    tabelas = {}
    for (nome,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"):
        tabelas[nome] = conn.execute(f'SELECT COUNT(*) FROM "{nome}"').fetchone()[0]

    rel = {
        "jobs": len(ids),
        "falhas": falhas,
        "eventos_de_candidatos_migrados": eventos,
        "jobs_data_bytes": {"antes": antes, "depois": depois},
        "arquivo_bytes": {"antes": tamanho_arquivo, "depois": db.stat().st_size},
        "chaves_restantes_em_data": dict(sorted(sobras.items())),
        "linhas_por_tabela": tabelas,
        "segundos": round(time.monotonic() - t0, 1),
    }
    return rel


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--sem-backup", action="store_true",
                    help="não faz a cópia de segurança antes (só para testes)")
    a = ap.parse_args(argv)
    rel = migrar(fazer_backup=not a.sem_backup)
    print(json.dumps(rel, ensure_ascii=False, indent=2))
    return 1 if rel["falhas"] else 0


if __name__ == "__main__":
    sys.exit(main())
