import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { Refresh, Search, Trash, Xmark } from 'iconoir-react'
import { api, fmtSize, post, type JobCounts, type JobListItem } from '../api'
import { Badge, Empty } from '../components/ui'

// jobTitle/kindLabel aceitam tanto o job completo quanto o item enxuto da lista
type JobLike = {
  movie: JobListItem['movie']; tmdb_id: number; kind?: string; language: string
  download_only?: boolean
}

export function jobTitle(j: JobLike): string {
  return j.movie ? `${j.movie.original_title} (${j.movie.year})` : `TMDB #${j.tmdb_id}`
}

// rótulo curto do tipo do job para exibir junto do idioma
export function kindLabel(j: JobLike): string {
  const dl = j.download_only ? ' · apenas baixar' : ''
  if (j.kind === 'original') return 'só original' + dl
  if (j.kind === 'dubbed') return `só dublado (${j.language})${dl}`
  return `${j.language} + orig${dl}`
}

// grupos do filtro; o backend filtra por grupo (não trazemos a lista toda)
type Filter = 'all' | 'active' | 'error' | 'done'

const FILTERS: { key: Filter; label: string }[] = [
  { key: 'active', label: 'Em andamento' },
  { key: 'error', label: 'Erro' },
  { key: 'done', label: 'Finalizado' },
  { key: 'all', label: 'Todos' },
]

export async function removeJob(id: string, reload: () => void) {
  if (!confirm('Remover este job?')) return
  const delT = confirm(
    'Apagar também os torrents e arquivos baixados no qBittorrent?\n\nOK = apagar tudo · Cancelar = manter os downloads',
  )
  try {
    await api(`/api/jobs/${id}?delete_torrents=${delT}`, { method: 'DELETE' })
    reload()
  } catch (e) {
    alert(`Erro: ${(e as Error).message}`)
  }
}

const EMPTY_COUNTS: JobCounts = { all: 0, active: 0, error: 0, done: 0 }

export default function Jobs() {
  const [jobs, setJobs] = useState<JobListItem[] | null>(null)
  const [counts, setCounts] = useState<JobCounts>(EMPTY_COUNTS)
  // abre em "Em andamento": a tela foca no que está rodando
  const [filter, setFilter] = useState<Filter>('active')
  const [query, setQuery] = useState('')
  const navigate = useNavigate()
  // guarda a contagem do grupo aberto no último tick, para detectar mudança
  const lastGroupCount = useRef<number | null>(null)

  // busca a lista do grupo atual no backend (filtro feito lá)
  const reload = useCallback(async (group: Filter) => {
    try {
      const list = await api<JobListItem[]>(`/api/jobs/list?group=${group}`)
      setJobs(list)
      lastGroupCount.current = list.length
    } catch {
      /* servidor reiniciando; próximo tick */
    }
  }, [])

  // ao trocar de filtro, recarrega a lista daquele grupo
  useEffect(() => {
    setJobs(null)
    void reload(filter)
  }, [filter, reload])

  // poll de contagens a cada 15s (badges sempre certos, sem baixar as listas).
  // Recarrega a lista atual quando:
  //  - o grupo aberto é 'active'/'all': o progresso/status muda ao vivo, então
  //    atualiza a cada tick de qualquer forma;
  //  - grupo terminal (error/done): só quando a contagem daquele grupo mudou
  //    (mudam só por ação — remover/retry/concluir), evitando requests à toa.
  useEffect(() => {
    async function tick() {
      try {
        const c = await api<JobCounts>('/api/jobs/counts')
        setCounts(c)
        const liveGroup = filter === 'active' || filter === 'all'
        const changed = lastGroupCount.current !== null && c[filter] !== lastGroupCount.current
        if (liveGroup || changed) void reload(filter)
      } catch {
        /* servidor reiniciando; próximo tick */
      }
    }
    void tick()
    const t = setInterval(tick, 15000)
    return () => clearInterval(t)
  }, [filter, reload])

  async function retry(id: string) {
    try {
      await post(`/api/jobs/${id}/retry`)
      void reload(filter)
    } catch (e) {
      alert(`Erro: ${(e as Error).message}`)
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
        <div className="flex flex-wrap gap-1.5">
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
                🎬
              </div>
            )}
            <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-2">
              <span className="flex-1 font-semibold">
                {jobTitle(j)}{' '}
                <small className="font-normal text-zinc-400">
                  [{kindLabel(j)}{j.mode === 'manual' ? ' · manual' : j.mode === 'files' ? ' · arquivos locais' : ''}
                  {j.destination_label ? ` · ${j.destination_label}` : ''}]
                </small>
              </span>
              <Badge status={j.status} />
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
              <IconBtn title="Remover job" onClick={() => removeJob(j.id, () => reload(filter))}>
                <Trash width={15} height={15} />
              </IconBtn>
            </div>
            <div className="mt-1.5 text-sm whitespace-pre-wrap text-zinc-400">{j.detail}</div>
            {(j.video_torrent || j.audio_torrent) && (
              <div className="mt-2 text-xs text-zinc-500">
                {j.video_torrent && (
                  <div className="truncate">
                    🎥 {j.video_torrent.title} ({j.video_torrent.seeders} seeds, {fmtSize(j.video_torrent.size)})
                  </div>
                )}
                {j.audio_torrent && (
                  <div className="truncate">
                    🔊 {j.audio_torrent.title} ({j.audio_torrent.seeders} seeds, {fmtSize(j.audio_torrent.size)})
                  </div>
                )}
              </div>
            )}
            {j.status === 'downloading' && (
              <>
                <MiniBar label="Vídeo" pct={j.progress.video} />
                <MiniBar label="Áudio" pct={j.progress.audio} />
              </>
            )}
            {j.status === 'merging' && <MiniBar label="Conversão" pct={j.progress.merge} color="purple" />}
            </div>
          </div>
        )
      })}
    </div>
  )
}

// barra de progresso enxuta (só %) para os cards da lista. Velocidade/ETA/seeds
// ficam no detalhe do job, não aqui.
function MiniBar({ label, pct, color = 'blue' }: {
  label: string
  pct: number | null
  color?: 'blue' | 'purple'
}) {
  if (pct == null) return null
  const bar = color === 'purple' ? 'bg-purple-500' : 'bg-blue-500'
  return (
    <div className="mt-2">
      <div className="h-2 overflow-hidden rounded bg-zinc-800">
        <div className={`h-full ${bar} transition-all duration-500`} style={{ width: `${pct}%` }} />
      </div>
      <div className="mt-1 text-xs text-zinc-400">
        {label}: {Math.round(pct)}%
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
