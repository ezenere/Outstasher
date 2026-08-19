import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { MediaVideo, Movie, Refresh, Search, SoundHigh, Trash, Xmark } from 'iconoir-react'
import {
  api, fmtSize, mergePhase, post,
  type JobCounts, type JobListItem, type JobListPage, type SlimProgress,
} from '../api'
import { Badge, ClampText, Empty, KindTags, Tag, torrentComplete, torrentSize } from '../components/ui'
import { useDialog, type DialogApi } from '../components/Dialog'

// jobTitle aceita tanto o job completo quanto o item enxuto da lista
type JobLike = {
  movie: JobListItem['movie']; tmdb_id: number; kind?: string; language: string
  download_only?: boolean
  convert?: boolean | object | null
}

export function jobTitle(j: JobLike): string {
  return j.movie ? `${j.movie.original_title} (${j.movie.year})` : `TMDB #${j.tmdb_id}`
}

// grupos do filtro; o backend filtra por grupo (não trazemos a lista toda)
type Filter = 'all' | 'active' | 'error' | 'done'

const FILTERS: { key: Filter; label: string }[] = [
  { key: 'active', label: 'Em andamento' },
  { key: 'error', label: 'Erro' },
  { key: 'done', label: 'Finalizado' },
  { key: 'all', label: 'Todos' },
]

// dimensão de mídia, ortogonal ao grupo de status (as duas combinam na query)
export type MediaFilter = 'all' | 'movie' | 'tv'

export const MEDIA_FILTERS: { key: MediaFilter; label: string }[] = [
  { key: 'movie', label: 'Filmes' },
  { key: 'tv', label: 'Séries' },
  { key: 'all', label: 'Todos' },
]

/** Query string do filtro de mídia ('' quando "Todos" — o backend não filtra). */
export function mediaQs(m: MediaFilter): string {
  return m === 'all' ? '' : `&media=${m}`
}

export async function removeJob(dialog: DialogApi, id: string, reload: () => void) {
  if (!(await dialog.confirm({
    title: 'Remover job',
    message: 'Remover este job do histórico?',
    confirmText: 'Remover', tone: 'danger',
  }))) return
  // segunda pergunta: apagar também os dados no qBittorrent?
  const delT = await dialog.confirm({
    title: 'Apagar os downloads?',
    message: 'Apagar também os torrents e arquivos baixados no qBittorrent?',
    confirmText: 'Apagar tudo', cancelText: 'Manter downloads', tone: 'danger',
  })
  try {
    await api(`/api/jobs/${id}?delete_torrents=${delT}`, { method: 'DELETE' })
    reload()
  } catch (e) {
    await dialog.alert({ title: 'Erro', message: (e as Error).message })
  }
}

const EMPTY_COUNTS: JobCounts = { all: 0, active: 0, error: 0, done: 0 }

const PER_PAGE = 20

export default function Jobs() {
  const [jobs, setJobs] = useState<JobListItem[] | null>(null)
  const [counts, setCounts] = useState<JobCounts>(EMPTY_COUNTS)
  // abre em "Em andamento": a tela foca no que está rodando
  const [filter, setFilter] = useState<Filter>('active')
  const [media, setMedia] = useState<MediaFilter>('all')
  const [query, setQuery] = useState('')
  const [page, setPage] = useState(1)
  const [pages, setPages] = useState(1)
  const navigate = useNavigate()
  const dialog = useDialog()
  // guarda a contagem do grupo aberto no último tick, para detectar mudança
  const lastGroupCount = useRef<number | null>(null)
  // buscando por texto? o filtro é no cliente, então precisamos do grupo INTEIRO
  // (senão a busca só acharia o que estivesse na página aberta)
  const searching = query.trim() !== ''

  // busca a lista do grupo atual no backend (filtro feito lá)
  const reload = useCallback(async (group: Filter, p: number, all: boolean, m: MediaFilter) => {
    try {
      const base = all ? `group=${group}` : `group=${group}&page=${p}&per_page=${PER_PAGE}`
      const res = await api<JobListPage>(`/api/jobs/list?${base}${mediaQs(m)}`)
      setJobs(res.items)
      setPages(res.pages)
      // a página pedida pode ter sumido (jobs removidos): volta para a última
      if (res.page > res.pages) setPage(res.pages)
      lastGroupCount.current = res.total
    } catch {
      /* servidor reiniciando; próximo tick */
    }
  }, [])

  // trocar qualquer filtro volta para a primeira página
  useEffect(() => {
    setPage(1)
  }, [filter, media])

  // recarrega ao mudar grupo, mídia, página ou ao entrar/sair da busca
  useEffect(() => {
    setJobs(null)
    void reload(filter, page, searching, media)
  }, [filter, media, page, searching, reload])

  // poll de contagens a cada 15s (badges sempre certos, sem baixar as listas).
  // Recarrega a lista atual quando:
  //  - o grupo aberto é 'active'/'all': o progresso/status muda ao vivo, então
  //    atualiza a cada tick de qualquer forma;
  //  - grupo terminal (error/done): só quando a contagem daquele grupo mudou
  //    (mudam só por ação — remover/retry/concluir), evitando requests à toa.
  useEffect(() => {
    async function tick() {
      try {
        // badges seguem o filtro de mídia ativo (contagens do que está visível)
        const mq = mediaQs(media)
        const c = await api<JobCounts>(`/api/jobs/counts${mq ? `?${mq.slice(1)}` : ''}`)
        setCounts(c)
        const liveGroup = filter === 'active' || filter === 'all'
        const changed = lastGroupCount.current !== null && c[filter] !== lastGroupCount.current
        if (liveGroup || changed) void reload(filter, page, searching, media)
      } catch {
        /* servidor reiniciando; próximo tick */
      }
    }
    void tick()
    const t = setInterval(tick, 15000)
    return () => clearInterval(t)
  }, [filter, media, page, searching, reload])

  async function retry(id: string) {
    try {
      await post(`/api/jobs/${id}/retry`)
      void reload(filter, page, searching, media)
    } catch (e) {
      await dialog.alert({ title: 'Erro', message: (e as Error).message })
    }
  }

  // a lista já vem filtrada por grupo do backend; aqui só o filtro de texto
  const filtered = (jobs ?? []).filter((j) => {
    const q = query.trim().toLowerCase()
    return !q || jobTitle(j).toLowerCase().includes(q)
  })

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center">
        <div className="flex flex-wrap items-center gap-1.5">
          {/* dimensão de mídia (filmes/séries) — combina com o grupo de status */}
          {MEDIA_FILTERS.map((m) => (
            <button
              key={m.key}
              onClick={() => setMedia(m.key)}
              className={`rounded-lg px-3 py-1.5 text-sm transition-colors ${
                media === m.key
                  ? 'bg-blue-600 font-semibold text-white'
                  : 'bg-zinc-800 text-zinc-300 hover:bg-zinc-700'
              }`}
            >
              {m.label}
            </button>
          ))}
          <span className="mx-1 h-5 w-px bg-zinc-700" />
          {FILTERS.map((f) => (
            <button
              key={f.key}
              onClick={() => setFilter(f.key)}
              className={`rounded-lg px-3 py-1.5 text-sm transition-colors ${
                filter === f.key
                  ? 'bg-blue-600 font-semibold text-white'
                  : 'bg-zinc-800 text-zinc-300 hover:bg-zinc-700'
              }`}
            >
              {f.label}
              <span className={`ml-1.5 ${filter === f.key ? 'text-blue-200' : 'text-zinc-500'}`}>
                {counts[f.key]}
              </span>
            </button>
          ))}
        </div>
        <div className="relative sm:ml-auto sm:w-64">
          <Search width={15} height={15} className="absolute top-1/2 left-3 -translate-y-1/2 text-zinc-500" />
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Buscar pelo nome do filme..."
            className="w-full rounded-lg border border-zinc-700 bg-zinc-900 py-2 pr-8 pl-9 text-sm outline-none focus:border-blue-500"
          />
          {query && (
            <button
              onClick={() => setQuery('')}
              className="absolute top-1/2 right-2 -translate-y-1/2 text-zinc-500 hover:text-zinc-300"
              title="Limpar"
            >
              <Xmark width={15} height={15} />
            </button>
          )}
        </div>
      </div>

      {jobs === null ? (
        <Empty>Carregando...</Empty>
      ) : filtered.length === 0 ? (
        <Empty>Nenhum job com esse filtro.</Empty>
      ) : null}

      {filtered.map((j) => {
        return (
          <div key={j.id} className="flex gap-3 rounded-xl bg-zinc-900 px-4 py-3.5">
            {j.movie?.poster ? (
              <img
                src={j.movie.poster}
                loading="lazy"
                className="hidden h-[167px] w-[111px] shrink-0 rounded-md bg-zinc-800 object-cover sm:block"
                alt=""
              />
            ) : (
              <div className="hidden h-[167px] w-[111px] shrink-0 items-center justify-center rounded-md bg-zinc-800 text-zinc-600 sm:flex">
                <Movie width={28} height={28} />
              </div>
            )}
            <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-2">
              <span className="flex-1 font-semibold">{jobTitle(j)}</span>
              <Badge status={j.status} phase={j.progress.merge_phase} />
              {j.status === 'awaiting' && (
                <button
                  onClick={() => navigate(`/jobs/${j.id}`)}
                  className="rounded-lg bg-blue-600 px-3 py-1.5 text-sm font-semibold hover:bg-blue-500"
                >
                  Escolher
                </button>
              )}
              {(j.status === 'error' || j.status === 'cancelled') && (
                <IconBtn title="Tentar de novo" onClick={() => retry(j.id)}>
                  <Refresh width={15} height={15} />
                </IconBtn>
              )}
              <Link to={`/jobs/${j.id}`} title="Ver detalhes"
                className="rounded-lg border border-zinc-700 p-1.5 text-zinc-400 hover:text-zinc-200">
                <Search width={15} height={15} />
              </Link>
              <IconBtn title="Remover job" onClick={() => removeJob(dialog, j.id, () => reload(filter, page, searching, media))}>
                <Trash width={15} height={15} />
              </IconBtn>
            </div>
            <div className="mt-1.5 flex flex-wrap items-center gap-1">
              {j.media_type === 'tv' && <Tag tone="info" title="Job de série">Série</Tag>}
              <KindTags kind={j.media_type === 'tv' ? undefined : j.kind} language={j.language}
                downloadOnly={j.download_only} convert={j.convert} mode={j.mode} />
              {j.media_type === 'tv' && j.series && (
                <span className="text-xs text-zinc-500">
                  · {j.series.episodes_total} ep.
                  {j.series.by_state.done ? ` · ${j.series.by_state.done} ok` : ''}
                  {j.series.by_state.failed ? ` · ${j.series.by_state.failed} falha(s)` : ''}
                </span>
              )}
              {j.destination_label && (
                <span className="text-xs text-zinc-500">· {j.destination_label}</span>
              )}
            </div>
            <ClampText className="mt-1.5 text-sm text-zinc-400">{j.detail}</ClampText>
            {(j.video_torrent || j.audio_torrent) && (
              <div className="mt-2 space-y-0.5 text-xs text-zinc-500">
                {j.video_torrent && (
                  <TorrentLine icon={MediaVideo} title={j.video_torrent.title}
                    size={j.progress.video?.size} />
                )}
                {j.audio_torrent && (
                  <TorrentLine icon={SoundHigh} title={j.audio_torrent.title}
                    size={j.progress.audio?.size} />
                )}
              </div>
            )}
            {j.status === 'downloading' && (
              j.media_type === 'tv' ? (
                <MiniBar label="Download" pct={j.series?.download_pct ?? null} color="blue" />
              ) : (
                <>
                  <TorrentBar label="Vídeo" p={j.progress.video} />
                  <TorrentBar label="Áudio" p={j.progress.audio} />
                </>
              )
            )}
            {j.status === 'merging' && (
              <MiniBar
                label={mergePhase(j.progress.merge_phase)?.label ?? 'Conversão'}
                pct={j.progress.merge}
                readPct={j.progress.merge_read}
                color={mergePhase(j.progress.merge_phase)?.amber ? 'amber' : 'purple'}
              />
            )}
            </div>
          </div>
        )
      })}

      {/* paginação: escondida durante a busca (lá a lista já vem inteira) */}
      {!searching && pages > 1 && (
        <div className="flex items-center justify-center gap-2 pt-1">
          <button
            onClick={() => setPage((p) => Math.max(1, p - 1))}
            disabled={page <= 1}
            className="rounded-lg border border-zinc-700 px-3 py-1.5 text-sm text-zinc-300 hover:bg-zinc-800 disabled:opacity-40 disabled:hover:bg-transparent"
          >
            Anterior
          </button>
          <span className="text-sm text-zinc-400">
            Página {page} de {pages}
          </span>
          <button
            onClick={() => setPage((p) => Math.min(pages, p + 1))}
            disabled={page >= pages}
            className="rounded-lg border border-zinc-700 px-3 py-1.5 text-sm text-zinc-300 hover:bg-zinc-800 disabled:opacity-40 disabled:hover:bg-transparent"
          >
            Próxima
          </button>
        </div>
      )}
    </div>
  )
}

/** Barra de um torrent no card: % + baixado/total. Velocidade/ETA/seeds ficam
 *  no detalhe do job. Torrent completo fica verde (o card ainda mostra o job
 *  como "baixando" enquanto o outro torrent não termina). */
function TorrentBar({ label, p }: { label: string; p: SlimProgress | null }) {
  if (!p) return null
  const complete = torrentComplete(p)
  const size = torrentSize(p)
  return (
    <div className="mt-2">
      <div className="h-2 overflow-hidden rounded bg-zinc-800">
        <div
          className={`h-full transition-all duration-500 ${complete ? 'bg-emerald-500' : 'bg-blue-500'}`}
          style={{ width: `${complete ? 100 : p.pct}%` }}
        />
      </div>
      <div className="mt-1 flex flex-wrap items-center gap-x-1.5 text-xs text-zinc-400">
        <span>{label}: {complete ? 'concluído' : `${Math.round(p.pct)}%`}</span>
        {size && <span className="text-zinc-500">· {size}</span>}
      </div>
    </div>
  )
}

// barra de progresso enxuta (só %) para os cards da lista. Velocidade/ETA/seeds
// ficam no detalhe do job, não aqui. Na conversão, `readPct` (frames lidos pelo
// encoder) vira uma barra clara sobreposta à de escrita (grande em AV1).
/** Linha do torrent no card: título + tamanho REAL (o do qBittorrent, que é
 *  o dos arquivos selecionados). O tamanho e os seeds do indexador quase nunca
 *  batem com o que foi baixado, então não aparecem aqui. */
function TorrentLine({ icon: Icon, title, size }: {
  icon: typeof MediaVideo
  title: string
  size?: number | null
}) {
  return (
    <div className="flex items-center gap-1 truncate">
      <Icon width={12} height={12} className="shrink-0" />
      <span className="truncate">{title}</span>
      {size ? <span className="shrink-0">({fmtSize(size)})</span> : null}
    </div>
  )
}


function MiniBar({ label, pct, readPct, color = 'blue' }: {
  label: string
  pct: number | null
  readPct?: number | null
  color?: 'blue' | 'purple' | 'amber'
}) {
  if (pct == null) return null
  const bar = { blue: 'bg-blue-500', purple: 'bg-purple-500', amber: 'bg-amber-500' }[color]
  const barSoft = { blue: 'bg-blue-500/30', purple: 'bg-purple-500/30',
                    amber: 'bg-amber-500/30' }[color]
  const read = Math.max(pct, readPct ?? pct)
  const buffering = read - pct > 1
  return (
    <div className="mt-2">
      <div className="relative h-2 overflow-hidden rounded bg-zinc-800">
        <div className={`absolute inset-y-0 left-0 ${barSoft} transition-all duration-500`} style={{ width: `${read}%` }} />
        <div className={`absolute inset-y-0 left-0 ${bar} transition-all duration-500`} style={{ width: `${pct}%` }} />
      </div>
      <div className="mt-1 text-xs text-zinc-400">
        {label}: {Math.round(pct)}%
        {buffering && <span className="text-purple-300/80"> (lido {Math.round(read)}%)</span>}
      </div>
    </div>
  )
}

function IconBtn({ title, onClick, children }: {
  title: string
  onClick: () => void
  children: React.ReactNode
}) {
  return (
    <button
      onClick={onClick}
      title={title}
      className="rounded-lg border border-zinc-700 p-1.5 text-zinc-400 hover:text-zinc-200"
    >
      {children}
    </button>
  )
}
