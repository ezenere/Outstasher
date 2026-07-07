import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { NavArrowDown } from 'iconoir-react'
import { api, MOVIE_STATE_LABEL, prog, type Job, type MovieState } from '../api'
import { MovieStateIcon } from './ui'
import { jobTitle } from '../pages/Jobs'

// mapeia o status do job para o estado visual (mesma paleta do card de filme).
// só considera o que está "em andamento" — done/cancelled ficam de fora do menu.
function jobToState(status: string): MovieState | null {
  if (status === 'merging') return 'converting'
  if (status === 'downloading') return 'downloading'
  if (status === 'searching') return 'searching'
  if (status === 'awaiting') return 'awaiting'
  if (status === 'error') return 'error'
  return null
}

/** Dropdown de processos em andamento no cabeçalho + bolinha de pendência.
 *  onPending informa ao App se algum job manual aguarda resposta (bolinha na aba). */
export default function ProcessMenu({ onPending }: { onPending?: (has: boolean) => void }) {
  const [jobs, setJobs] = useState<Job[]>([])
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)

  useEffect(() => {
    async function load() {
      try {
        setJobs(await api<Job[]>('/api/jobs'))
      } catch {
        /* servidor reiniciando; próxima tentativa no tick */
      }
    }
    void load()
    const t = setInterval(load, 4000)
    return () => clearInterval(t)
  }, [])

  // fecha ao clicar fora
  useEffect(() => {
    if (!open) return
    function onClick(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', onClick)
    return () => document.removeEventListener('mousedown', onClick)
  }, [open])

  // só os que estão "em andamento", com prioridade convertendo > baixando > aguardando > erro
  const active = jobs
    .map((j) => ({ j, state: jobToState(j.status) }))
    .filter((x): x is { j: Job; state: MovieState } => x.state !== null)
    .sort((a, b) => RANK[a.state] - RANK[b.state])

  const pending = active.some((x) => x.state === 'awaiting')

  useEffect(() => {
    onPending?.(pending)
  }, [pending, onPending])

  return (
    <div className="relative" ref={ref}>
      <button
        onClick={() => setOpen((v) => !v)}
        title="Processos em andamento"
        className="relative flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-sm text-zinc-400 transition-colors hover:text-zinc-200"
      >
        Processos
        {active.length > 0 && (
          <span className="rounded-full bg-zinc-700 px-1.5 text-xs text-zinc-200">{active.length}</span>
        )}
        <NavArrowDown width={14} height={14} />
        {pending && (
          <span className="absolute -top-0.5 -right-0.5 h-2.5 w-2.5 rounded-full bg-red-500 ring-2 ring-zinc-950" />
        )}
      </button>

      {open && (
        <div className="absolute right-0 z-30 mt-2 w-80 overflow-hidden rounded-xl border border-zinc-800 bg-zinc-900 shadow-xl">
          {active.length === 0 ? (
            <div className="px-4 py-3 text-sm text-zinc-500">Nenhum processo em andamento.</div>
          ) : (
            <ul className="max-h-96 overflow-auto">
              {active.map(({ j, state }) => {
                const p = prog(j.progress?.video) ?? prog(j.progress?.audio)
                const pct = state === 'converting' ? j.progress?.merge?.pct : p?.pct
                return (
                  <li key={j.id} className="border-b border-zinc-800/60 last:border-0">
                    <Link
                      to={`/jobs/${j.id}`}
                      onClick={() => setOpen(false)}
                      className="flex items-start gap-2 px-3 py-2.5 hover:bg-zinc-800/50"
                    >
                      <MovieStateIcon state={state} className="mt-0.5 shrink-0" />
                      <div className="min-w-0 flex-1">
                        <div className="truncate text-sm font-medium text-zinc-200">{jobTitle(j)}</div>
                        <div className="flex items-center gap-1.5 text-xs text-zinc-400">
                          {MOVIE_STATE_LABEL[state]}
                          {pct != null && <span className="tabular-nums">· {Math.round(pct)}%</span>}
                          {state === 'awaiting' && (
                            <span className="font-semibold text-red-400">· precisa de resposta</span>
                          )}
                        </div>
                      </div>
                    </Link>
                  </li>
                )
              })}
            </ul>
          )}
        </div>
      )}
    </div>
  )
}

const RANK: Record<MovieState, number> = {
  converting: 0,
  downloading: 1,
  searching: 2,
  awaiting: 3,
  done: 4,
  error: 5,
}
