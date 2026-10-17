"""Persistencia em SQLite (jobs.db).

- jobs: uma linha por job — os escalares em colunas e, em jobs.data, só a
  configuração (convert, advanced_merge, review_rules) e o que não coube no
  formato; as listas padronizadas em tabelas próprias (ver upsert_job):
  search_queries / search_candidates / candidate_episodes (candidatos) e,
  nos jobs de série, job_episodes / episode_subs / job_torrents /
  torrent_coverage / torrent_files; as EDLs do alinhador (série e filme) em
  edls / edl_segments; progresso do download do filme, pedido da série,
  merge manual, lote de recompressão e perfil de drift em tabelas pequenas.
- metadados do TMDB (títulos, pôster, temporadas, ordens alternativas): uma
  vez por obra em media / media_localized / media_seasons /
  media_episode_groups, compartilhados pelos jobs do mesmo tmdb_id.
- configuração: chave/valor em settings e JSON opaco onde é forma livre
  (presets, merge avançado); as listas da configuração em tabelas —
  language_markers (por idioma), subtitle_markers e search_rules.
- events: append-only, um INSERT por evento — nada de regravar tudo.
- WAL + lock: seguro para o event loop e para a thread do merge.
- Migracao automatica: se existir jobs.json (formato antigo), importa e
  renomeia para jobs.json.bak.

O pacote (quem chama usa só `from services import store` e `store.X`):
  db             conexão, schema base e init()
  configuration  settings, regras de busca extra, presets, destinos, alvos
                 de torrent, idiomas e marcadores
  events         eventos do job
  jobs           upsert/get/load/delete do job: decomposição nas seções,
                 detecção de mudança por hash e recomposição
  candidates     candidatos de busca
  series         episódios e torrents (série)
  edls           EDLs do alinhador
  scalars        escalares do job e metadados do TMDB (os codecs; o SQL
                 dessas linhas em scalar_rows)
  common         utilitários das seções (JSON, hash, lotes de ids)
Cada seção traz as tabelas dela, o codec linha <-> dict e a normalização do
formato antigo (as funções _legacy_*).

Estado compartilhado (a conexão, o lock e o cache de hashes das seções) mora
AQUI, e só aqui: os submódulos o leem como `store._conn`, `store._lock`,
`store._section_hash` NA HORA da chamada — nunca `from . import _conn`, que
congelaria o valor da importação. Assim `importlib.reload(store)` (a fixture
temp_db dos testes) zera o estado de verdade, e `store._conn = None` seguido
de `store.init()` (reabrir o banco) vale para todos os submódulos.
"""
import sqlite3
import threading

from .candidates import _LEGACY_KEYS, _legacy_candidates
from .common import _digest
from .configuration import (
    EXTRA_SEARCH_VARIANTS, _legacy_markers, _legacy_search_rules,
    add_destination, add_torrent_target, create_convert_preset,
    default_destination, default_torrent_target, delete_convert_preset,
    delete_destination, delete_torrent_target, get_destination,
    get_extra_search_rules, get_setting, get_subtitle_markers,
    get_torrent_target, list_convert_presets, list_destinations,
    list_destinations_by_media, list_languages, list_torrent_targets,
    load_language_config, save_languages, set_extra_search_rules,
    set_setting, update_destination, update_torrent_target,
)
from .db import init
from .edls import (
    EDL_MOVIE_KEY, EDLS_UNLOADED, _edl_round_trip, _has_legacy_edls,
    _legacy_edls, _profile_blob, _profile_from_blob,
)
from .events import MAX_EVENTS_RETURNED, add_event, load_events
from .jobs import (
    LIST_SECTIONS, SECTIONS, _apply_upsert, _prepare_upsert, _remember,
    count_jobs_by_status, delete_job, error_jobs_for, get_job, load_jobs,
    load_jobs_by_status, migrate_candidate_events, upsert_job,
)
from .scalars import (
    _DERIVED_TV_GATES, _KID_TABLES, _MOVIE_KEYS, _PROG_TYPES, _encode, _fit,
    _legacy_scalars,
)
from .series import _PROGRESS_KEYS, _legacy_series

# -------------------- estado compartilhado --------------------

_conn: sqlite3.Connection | None = None
_lock = threading.Lock()

# hash das últimas linhas GRAVADAS das seções de cada job: o upsert só
# reescreve as linhas quando elas mudaram (a maioria das gravações do job é
# progresso/estado — candidatos mudam na busca, na escolha e nas trocas).
#   {job_id: {"candidates": hash da seção,
#             "episodes": {ep_key: hash da linha},
#             "torrents": {n: (hash da linha, hash de cobertura+arquivos)},
#             "edls": {ep_key | "filme": hash da EDL},
#             "scalars": {tabela filha | "media" | "seasons" | "groups": hash}}}
# Candidatos mudam em bloco e a seção inteira é reescrita; episódios,
# torrents e EDLs mudam um de cada vez (o merge conclui UM episódio, o
# watchdog atualiza o progresso de UM torrent, a revisão decide a EDL de UM
# episódio), então o hash é por linha e só a linha que mudou é regravada.
# As tabelas pequenas dos escalares (progresso do filme, pedido...) e os
# metadados compartilhados têm um hash cada. Sem entrada para a seção
# (processo novo, ou o job veio do formato antigo), o próximo upsert
# reescreve a seção inteira.
_section_hash: dict[str, dict] = {}
