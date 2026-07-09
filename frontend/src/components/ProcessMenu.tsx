import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { NavArrowDown } from 'iconoir-react'
import { MOVIE_STATE_LABEL, type JobSummary, type MovieState } from '../api'
import { MovieStateIcon } from './ui'

/** Dropdown de processos em andamento no cabeçalho + bolinha de pendência.
 *  O App é a fonte do summary (polling de 5s) e passa `items` aqui; este
 *  componente só renderiza. onPending informa ao App se algum job manual
 *  aguarda resposta (bolinha na aba). */
export default function ProcessMenu({
  items,
  onPending,
}: {
  items: JobSummary[]
  onPending?: (has: boolean) => void
}) {
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)

  // fecha ao clicar fora
  useEffect(() => {
    if (!open) return
    function onClick(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', onClick)
    return () => document.removeEventListener('mousedown', onClick)
  }, [open])

  // o backend já devolve só o que interessa (em andamento + erro), ordenado
  const active = items
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
              {active.map((j) => (
                <li key={j.id} className="border-b border-zinc-800/60 last:border-0">
                  <Link
                    to={`/jobs/${j.id}`}
                    onClick={() => setOpen(false)}
                    className="flex items-start gap-2 px-3 py-2.5 hover:bg-zinc-800/50"
                  >
                    <MovieStateIcon state={j.state as MovieState} className="mt-0.5 shrink-0" />
                    <div className="min-w-0 flex-1">
                      <div className="truncate text-sm font-medium text-zinc-200">{j.title}</div>
                      <div className="flex items-center gap-1.5 text-xs text-zinc-400">
                        {MOVIE_STATE_LABEL[j.state as MovieState]}
                        {j.pct != null && <span className="tabular-nums">· {Math.round(j.pct)}%</span>}
                        {j.state === 'awaiting' && (
                          <span className="font-semibold text-red-400">· precisa de resposta</span>
                        )}
                      </div>
                    </div>
                  </Link>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  )
}
