import type { ReactNode } from 'react'
import type { Candidate, Progress } from '../api'
import { fmtEta, fmtSize, fmtSpeed, STATUS_LABEL } from '../api'
import { Check } from 'iconoir-react'

const BADGE_STYLES: Record<string, string> = {
  searching: 'bg-blue-950 text-blue-400',
  awaiting: 'bg-purple-950 text-purple-400',
  downloading: 'bg-yellow-950 text-yellow-400',
  merging: 'bg-blue-950 text-blue-400',
  done: 'bg-emerald-950 text-emerald-400',
  error: 'bg-red-950 text-red-400',
  cancelled: 'bg-zinc-800 text-zinc-400',
}

export function Badge({ status }: { status: string }) {
  return (
    <span className={`rounded-full px-2.5 py-0.5 text-xs font-semibold whitespace-nowrap ${BADGE_STYLES[status] ?? 'bg-zinc-800 text-zinc-300'}`}>
      {STATUS_LABEL[status] ?? status}
    </span>
  )
}

export function ProgressBar({ label, p }: { label: string; p: Progress | null }) {
  if (!p) return null
  const extra = [
    p.speed ? fmtSpeed(p.speed) : null,
    p.eta != null && p.eta < 8640000 && p.pct < 100 ? `ETA ${fmtEta(p.eta)}` : null,
    p.seeds != null ? `${p.seeds} seeds` : null,
    p.state || null,
  ]
    .filter(Boolean)
    .join(' · ')
  return (
    <div className="mt-2">
      <div className="h-2 overflow-hidden rounded bg-zinc-800">
        <div className="h-full bg-blue-500 transition-all duration-500" style={{ width: `${p.pct || 0}%` }} />
      </div>
      <div className="mt-1 text-xs text-zinc-400">
        {label}: {p.pct || 0}%{extra ? ` — ${extra}` : ''}
      </div>
    </div>
  )
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="py-3 text-zinc-500">{children}</div>
}

interface CandidatesTableProps {
  candidates: Candidate[]
  selectable?: boolean
  selectedId?: string
  onSelect?: (id: string) => void
  showReason?: boolean
}

export function CandidatesTable({ candidates, selectable, selectedId, onSelect, showReason }: CandidatesTableProps) {
  if (!candidates.length) return <Empty>Nenhum candidato.</Empty>
  return (
    <div className="mb-2 max-h-72 overflow-auto rounded-lg border border-zinc-800">
      <table className="w-full border-collapse text-xs">
        <thead>
          <tr className="sticky top-0 bg-zinc-900 text-left text-zinc-400">
            <th className="px-2 py-1.5" />
            <th className="px-2 py-1.5">Título</th>
            <th className="px-2 py-1.5">Tracker</th>
            <th className="px-2 py-1.5">Seeds</th>
            <th className="px-2 py-1.5">Tamanho</th>
            <th className="px-2 py-1.5">Corte</th>
            <th className="px-2 py-1.5">Score</th>
            {showReason && <th className="px-2 py-1.5">Motivo</th>}
          </tr>
        </thead>
        <tbody>
          {candidates.map((c, i) => {
            const rowCls = c.chosen
              ? 'text-emerald-400 font-semibold'
              : c.rejected
                ? 'text-zinc-500'
                : ''
            const clickable = selectable && c.id
            return (
              <tr
                key={c.id ?? i}
                className={`border-t border-zinc-800/60 ${rowCls} ${clickable ? 'cursor-pointer hover:bg-zinc-800/40' : ''} ${selectable && selectedId === c.id ? 'bg-blue-950/40' : ''}`}
                onClick={clickable ? () => onSelect?.(c.id!) : undefined}
              >
                <td className="px-2 py-1.5">
                  {selectable && c.id ? (
                    <input type="radio" checked={selectedId === c.id} onChange={() => onSelect?.(c.id!)} />
                  ) : c.chosen ? (
                    <Check width={14} height={14} className="text-emerald-400" />
                  ) : null}
                </td>
                <td className="max-w-96 truncate px-2 py-1.5" title={c.title}>{c.title}</td>
                <td className="px-2 py-1.5">{c.tracker ?? ''}</td>
                <td className="px-2 py-1.5">{c.seeders}</td>
                <td className="px-2 py-1.5">{fmtSize(c.size)}</td>
                <td className="px-2 py-1.5">{c.edition ?? 'normal'}</td>
                <td className="px-2 py-1.5">{c.score ?? '—'}</td>
                {showReason && (
                  <td className="px-2 py-1.5">{c.rejected ?? (c.chosen ? 'escolhido' : 'ok')}</td>
                )}
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}
