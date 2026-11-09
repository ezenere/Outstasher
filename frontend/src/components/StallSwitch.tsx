import { useEffect, useState } from 'react'
import { setStallSwitch, type StallClock, type StallMode, type StallSwitchConfig } from '../api'
import { useDialog } from './Dialog'
import { Toggle } from './ui'

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

/** Linha do relógio da troca, sob os botões do torrent (some enquanto ele anda). */
export function StallClockLine({ clock }: { clock?: StallClock }) {
  const text = stallText(clock)
  if (!text) return null
  return (
    <div className={`mt-1 text-xs ${clock?.no_backup ? 'text-amber-400' : 'text-zinc-500'}`}>
      {text}
    </div>
  )
}

/**
 * Troca automática de torrent travado em UM torrent do job, na mesma linha
 * dos botões de troca: "Troca automática [liga/desliga] em [15] min".
 *
 * Sem mexer, o torrent HERDA a global — o interruptor mostra o estado dela e
 * o tempo vem preenchido com o dela. Desligar é explícito ('off': não passa a
 * trocar se a global for ligada depois); religar volta a herdar quando a
 * global está ligada. Tempo diferente do global vira o tempo deste torrent
 * (vale em qualquer estado); igual ao global, volta a usar os limites dela.
 * O campo de tempo só aparece com a troca ligada. O watchdog relê a cada
 * volta: muda já no próximo tick.
 */
export default function StallSwitchControl({ jobId, target, mode, minutes, global, onChanged }: {
  jobId: string
  /** filme: o papel; série: o n do torrent do plano */
  target: { kind: 'video' | 'audio' } | { n: number }
  mode?: StallMode
  minutes?: number
  global?: StallSwitchConfig | null
  onChanged: () => void
}) {
  const dialog = useDialog()
  const [saving, setSaving] = useState(false)
  const globalMin = global?.minutes ?? null
  const shown = minutes ?? globalMin
  const [mins, setMins] = useState(shown != null ? String(shown) : '')
  useEffect(() => setMins(shown != null ? String(shown) : ''), [shown])

  const on = mode === 'on' || (mode == null && !!global?.enabled)

  async function save(nextMode: StallMode | 'inherit', nextMinutes: number | null) {
    setSaving(true)
    try {
      await setStallSwitch(jobId, { ...target, mode: nextMode, minutes: nextMinutes })
      onChanged()
    } catch (e) {
      await dialog.alert({ title: 'Erro', message: (e as Error).message })
      setMins(shown != null ? String(shown) : '')
    } finally {
      setSaving(false)
    }
  }

  function toggle(next: boolean) {
    if (!next) void save('off', minutes ?? null)
    // religar: com a global ligada volta a herdar (o padrão); desligada,
    // liga só neste torrent
    else void save(global?.enabled ? 'inherit' : 'on', minutes ?? null)
  }

  function commitMinutes() {
    const v = mins.trim()
    const n = v ? Number(v) : null
    if (n != null && (!Number.isInteger(n) || n < 1)) {
      setMins(shown != null ? String(shown) : '')
      return
    }
    // vazio ou igual ao da global: sem tempo próprio (limites da global)
    const next = n == null || n === globalMin ? null : n
    if (next === (minutes ?? null)) {
      setMins(shown != null ? String(shown) : '')
      return
    }
    void save(mode ?? 'inherit', next)
  }

  const herda = mode == null
  const dica = global
    ? `${herda ? 'Herdando a global' : 'Ajuste deste torrent'} — a global ${
      global.enabled ? 'está ligada' : 'está desligada'}. Troca sozinho por um candidato reserva quando este torrent fica sem progresso.`
    : 'Troca sozinho por um candidato reserva quando este torrent fica sem progresso.'
  return (
    <div className="ml-auto flex items-center gap-1.5 text-xs text-zinc-400" title={dica}>
      <span>Troca automática</span>
      <Toggle size="sm" checked={on} disabled={saving} onChange={toggle} />
      {on && (
        <label className="flex items-center gap-1"
          title={global
            ? `Minutos sem progresso até trocar. Igual ao global (${global.minutes} min): usa os limites da global — ${global.minutes} min baixando, ${global.metadata_minutes} buscando metadados, ${global.stalled_minutes} sem fontes. Diferente: vale em qualquer estado.`
            : 'Minutos sem progresso até trocar'}>
          em
          <input
            type="number"
            min={1}
            value={mins}
            disabled={saving}
            onChange={(e) => setMins(e.target.value)}
            onBlur={commitMinutes}
            onKeyDown={(e) => { if (e.key === 'Enter') (e.target as HTMLInputElement).blur() }}
            className={`w-14 rounded-lg border bg-zinc-800 px-1.5 py-0.5 text-xs tabular-nums outline-none focus:border-blue-500 ${
              minutes != null ? 'border-blue-800 text-zinc-100' : 'border-zinc-700'}`}
          />
          min
        </label>
      )}
    </div>
  )
}
