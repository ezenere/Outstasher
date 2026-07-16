import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { MediaVideo, Movie, Refresh, Search, SoundHigh, Trash, Xmark } from 'iconoir-react'
import { api, fmtSize, post, type JobCounts, type JobListItem } from '../api'
import { Badge, ClampText, Empty, KindTags } from '../components/ui'
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

export default function Jobs() {
  const [jobs, setJobs] = useState<JobListItem[] | null>(null)
  const [counts, setCounts] = useState<JobCounts>(EMPTY_COUNTS)
  // abre em "Em andamento": a tela foca no que está rodando
  const [filter, setFilter] = useState<Filter>('active')
  const [query, setQuery] = useState('')
  const navigate = useNavigate()
  const dialog = useDialog()
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
                <Movie width={28} height={28} />
              </div>
            )}
            <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-2">
              <span className="flex-1 font-semibold">{jobTitle(j)}</span>
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
              <IconBtn title="Remover job" onClick={() => removeJob(dialog, j.id, () => reload(filter))}>
                <Trash width={15} height={15} />
              </IconBtn>
            </div>
            <div className="mt-1.5 flex flex-wrap items-center gap-1">
              <KindTags kind={j.kind} language={j.language} downloadOnly={j.download_only}
                convert={j.convert} mode={j.mode} />
              {j.destination_label && (
                <span className="text-xs text-zinc-500">· {j.destination_label}</span>
              )}
            </div>
            <ClampText className="mt-1.5 text-sm text-zinc-400">{j.detail}</ClampText>
            {(j.video_torrent || j.audio_torrent) && (
              <div className="mt-2 space-y-0.5 text-xs text-zinc-500">
                {j.video_torrent && (
                  <div className="flex items-center gap-1 truncate">
                    <MediaVideo width={12} height={12} className="shrink-0" />
                    <span className="truncate">{j.video_torrent.title}</span>
                    <span className="shrink-0">({j.video_torrent.seeders} seeds, {fmtSize(j.video_torrent.size)})</span>
                  </div>
                )}
                {j.audio_torrent && (
                  <div className="flex items-center gap-1 truncate">
                    <SoundHigh width={12} height={12} className="shrink-0" />
                    <span className="truncate">{j.audio_torrent.title}</span>
                    <span className="shrink-0">({j.audio_torrent.seeders} seeds, {fmtSize(j.audio_torrent.size)})</span>
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
            {j.status === 'merging' && (
              <MiniBar label="Conversão" pct={j.progress.merge} readPct={j.progress.merge_read} color="purple" />
            )}
            </div>
          </div>
        )
      })}
    </div>
  )
}

// barra de progresso enxuta (só %) para os cards da lista. Velocidade/ETA/seeds
// ficam no detalhe do job, não aqui. Na conversão, `readPct` (frames lidos pelo
// encoder) vira uma barra clara sobreposta à de escrita (grande em AV1).
function MiniBar({ label, pct, readPct, color = 'blue' }: {
  label: string
  pct: number | null
  readPct?: number | null
  color?: 'blue' | 'purple'
}) {
  if (pct == null) return null
  const bar = color === 'purple' ? 'bg-purple-500' : 'bg-blue-500'
  const barSoft = color === 'purple' ? 'bg-purple-500/30' : 'bg-blue-500/30'
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
