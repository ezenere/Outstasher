import { useEffect, useState } from 'react'
import { setStallSwitch, type StallClock, type StallMode, type StallSwitchConfig } from '../api'
import { useDialog } from './Dialog'

/** Duração curta: "9 min", "2 h", "1 h 30 min". */
function dur(s: number): string {
  const m = Math.max(0, Math.round(s / 60))
  if (m < 60) return `${m} min`
  const h = Math.floor(m / 60)
  return m % 60 ? `${h} h ${m % 60} min` : `${h} h`
}

/** O relógio do watchdog em texto: "sem progresso há 9 min — troca em 6 min".
 *  null enquanto o torrent anda (nada a dizer). */
export function stallText(c: StallClock | undefined): string | null {
  if (!c) return null
  if (c.paused) return 'parado no qBittorrent — o relógio da troca não conta'
  if (c.idle_s < 60) return null
  const head = `sem progresso há ${dur(c.idle_s)}`
  if (c.limit_s == null) return `${head} — troca automática desligada`
  if (c.no_backup) return `${head} — sem reserva para trocar, seguindo esperando`
  if (c.idle_s < c.limit_s) return `${head} — troca em ${dur(c.limit_s - c.idle_s)}`
  return `${head} — trocando…`
}

/**
 * Troca automática de torrent travado em UM torrent do job: herdar a global
 * (padrão) / ligada / desligada, e minutos próprios (valem em qualquer estado
 * do torrent). O watchdog relê a cada volta: muda já no próximo tick.
 */
export default function StallSwitchControl({ jobId, target, mode, minutes, clock, global, onChanged }: {
  jobId: string
  /** filme: o papel; série: o n do torrent do plano */
  target: { kind: 'video' | 'audio' } | { n: number }
  mode?: StallMode
  minutes?: number
  clock?: StallClock
  global?: StallSwitchConfig | null
  onChanged: () => void
}) {
  const dialog = useDialog()
  const [saving, setSaving] = useState(false)
  const [mins, setMins] = useState(minutes != null ? String(minutes) : '')
  useEffect(() => setMins(minutes != null ? String(minutes) : ''), [minutes])

  const effectiveOn = mode === 'on' || (mode == null && !!global?.enabled)
  const inheritLabel = global
    ? `Herdar (${global.enabled ? `ligada · ${global.minutes} min` : 'desligada'})`
    : 'Herdar da global'

  async function save(nextMode: StallMode | 'inherit', nextMinutes: number | null) {
    setSaving(true)
    try {
      await setStallSwitch(jobId, { ...target, mode: nextMode, minutes: nextMinutes })
      onChanged()
    } catch (e) {
      await dialog.alert({ title: 'Erro', message: (e as Error).message })
      setMins(minutes != null ? String(minutes) : '')
    } finally {
      setSaving(false)
    }
  }

  function commitMinutes() {
    const v = mins.trim()
    const n = v ? Number(v) : null
    if (n === (minutes ?? null)) return
    if (n != null && (!Number.isInteger(n) || n < 1)) {
      setMins(minutes != null ? String(minutes) : '')
      return
    }
    void save(mode ?? 'inherit', n)
  }

  const text = stallText(clock)
  return (
    <div className="mt-1.5 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-zinc-400">
      <span>Troca automática:</span>
      <select
        value={mode ?? 'inherit'}
        disabled={saving}
        onChange={(e) => void save(e.target.value as StallMode | 'inherit', minutes ?? null)}
        title="Trocar sozinho por um candidato reserva quando este torrent fica sem progresso"
        className="rounded-lg border border-zinc-700 bg-zinc-800 px-1.5 py-0.5 text-xs outline-none focus:border-blue-500"
      >
        <option value="inherit">{inheritLabel}</option>
        <option value="on">Ligada</option>
        <option value="off">Desligada</option>
      </select>
      {effectiveOn && (
        <label className="flex items-center gap-1" title="Minutos sem progresso até trocar, em qualquer estado do torrent (vazio = os da global)">
          após
          <input
            type="number"
            min={1}
            value={mins}
            disabled={saving}
            onChange={(e) => setMins(e.target.value)}
            onBlur={commitMinutes}
            onKeyDown={(e) => { if (e.key === 'Enter') (e.target as HTMLInputElement).blur() }}
            placeholder={global ? String(global.minutes) : 'global'}
            className="w-16 rounded-lg border border-zinc-700 bg-zinc-800 px-1.5 py-0.5 text-xs tabular-nums outline-none focus:border-blue-500"
          />
          min
        </label>
      )}
      {text && <span className={clock?.no_backup ? 'text-amber-400' : 'text-zinc-500'}>· {text}</span>}
    </div>
  )
}
