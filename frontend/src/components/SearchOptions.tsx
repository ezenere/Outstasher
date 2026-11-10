import { useEffect, useState } from 'react'
import {
  api, fmtWhen, MIN_RES_OPTIONS, MIN_SOURCE_OPTIONS, rescheduleJob, searchNow,
  setJobRetryMode, type Job, type MinQuality, type MinQualityByRole, type RetryMode,
  type SearchRetryConfig,
} from '../api'
import { Calendar, Refresh } from 'iconoir-react'
import { useDialog } from './Dialog'
import { Toggle } from './ui'

/** Política global da busca contínua (null enquanto carrega). */
export function useSearchRetryGlobal(): SearchRetryConfig | null {
  const [cfg, setCfg] = useState<SearchRetryConfig | null>(null)
  useEffect(() => {
    api<{ config: SearchRetryConfig }>('/api/search-retry')
      .then((r) => setCfg(r.config)).catch(() => {})
  }, [])
  return cfg
}

/** "2026-11-10T20:00" de agora + `h` horas, no formato do datetime-local. */
export function localInput(offsetHours = 1): string {
  const d = new Date(Date.now() + offsetHours * 3600_000)
  d.setMinutes(0, 0, 0)
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`
}

const fieldCls = 'rounded-lg border border-zinc-700 bg-zinc-800 px-2 py-1 text-sm'

/** "Agendar download [liga/desliga]" + data/hora quando ligado. */
export function ScheduleField({ value, onChange, label = 'Agendar download' }: {
  /** null = desligado (busca já) */
  value: string | null
  onChange: (v: string | null) => void
  label?: string
}) {
  return (
    <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-sm text-zinc-300">
      <label className="flex items-center gap-1.5">
        <Toggle checked={value !== null} onChange={(on) => onChange(on ? localInput(24) : null)} />
        {label}
      </label>
      {value !== null && (
        <input type="datetime-local" value={value} onChange={(e) => onChange(e.target.value || null)}
          className={fieldCls} />
      )}
    </div>
  )
}

/** Interruptor da busca contínua de um job (criação): sem mexer, herda a
 *  global e mostra o estado dela; desligar é explícito ('off'); religar volta
 *  a herdar quando a global está ligada. */
export function RetryToggle({ mode, onChange, global, size = 'md' }: {
  mode: RetryMode | null
  onChange: (m: RetryMode | null) => void
  global: SearchRetryConfig | null
  size?: 'sm' | 'md'
}) {
  const on = mode === 'on' || (mode == null && !!global?.enabled)
  return (
    <label className="flex items-center gap-1.5 text-sm text-zinc-300"
      title={`Se a busca não achar nada que sirva, tenta de novo a cada ${global ? every(global.minutes) : '…'} até achar ou o job ser excluído`}>
      <Toggle size={size} checked={on}
        onChange={(v) => onChange(v ? (global?.enabled ? null : 'on') : 'off')} />
      Busca contínua
      {global && on && <span className="text-xs text-zinc-500">(a cada {every(global.minutes)})</span>}
    </label>
  )
}

/** "6 h", "1 dia", "45 min". */
export function every(min: number): string {
  if (min % 1440 === 0) return min === 1440 ? '1 dia' : `${min / 1440} dias`
  if (min % 60 === 0) return `${min / 60} h`
  return min > 60 ? `${Math.floor(min / 60)} h ${min % 60} min` : `${min} min`
}

function MinRow({ label, value, onChange }: {
  label: string
  value: MinQuality | null | undefined
  onChange: (v: MinQuality | null) => void
}) {
  const res = value?.res ?? null
  const source = value?.source ?? null
  const set = (r: MinQuality['res'], s: MinQuality['source']) =>
    onChange(r || s ? { res: r, source: s } : null)
  return (
    <div className="flex flex-wrap items-center gap-2 text-sm">
      <span className="w-20 text-zinc-400">{label}</span>
      <select value={res ?? ''} className={fieldCls}
        onChange={(e) => set((e.target.value || null) as MinQuality['res'], source)}>
        {MIN_RES_OPTIONS.map(([v, l]) => <option key={l} value={v ?? ''}>{l}</option>)}
      </select>
      <select value={source ?? ''} className={fieldCls}
        onChange={(e) => set(res, (e.target.value || null) as MinQuality['source'])}>
        {MIN_SOURCE_OPTIONS.map(([v, l]) => <option key={l} value={v ?? ''}>{l}</option>)}
      </select>
    </div>
  )
}

/** Qualidade mínima da escolha automática, por lado. O dublado é medido
 *  pelo VÍDEO do release dublado — o nome quase nunca diz o áudio, mas um
 *  1080p WEB-DL traz o áudio do stream e um 480p rip não. */
export function MinQualityFields({ value, onChange, roles = ['video', 'audio'] }: {
  value: MinQualityByRole
  onChange: (v: MinQualityByRole) => void
  roles?: ('video' | 'audio')[]
}) {
  return (
    <div className="flex flex-col gap-1.5">
      <div className="text-xs text-zinc-500">
        Qualidade mínima — a escolha automática ignora o que ficar abaixo
        (com a busca contínua, espera aparecer)
      </div>
      {roles.includes('video') && (
        <MinRow label="Original" value={value.video}
          onChange={(v) => onChange({ ...value, video: v })} />
      )}
      {roles.includes('audio') && (
        <MinRow label="Dublado" value={value.audio}
          onChange={(v) => onChange({ ...value, audio: v })} />
      )}
    </div>
  )
}

/** O que vai no corpo da criação (null quando nada foi pedido). */
export function minQualityBody(v: MinQualityByRole, roles: ('video' | 'audio')[]) {
  const out: MinQualityByRole = {}
  for (const r of roles) if (v[r]) out[r] = v[r]
  return Object.keys(out).length ? out : null
}

/** "2026-11-10T20:00" do datetime-local -> o que o backend recebe (local). */
export function scheduleBody(v: string | null): string | null {
  return v ? (v.length === 16 ? `${v}:00` : v) : null
}

/** Painel do job programado (detalhe): quando busca, por quê, e as ações —
 *  buscar agora, mudar a data (antes da primeira busca) e a busca contínua. */
export function SchedulePanel({ job, global, onChanged }: {
  job: Job
  global: SearchRetryConfig | null
  onChanged: () => void
}) {
  const dialog = useDialog()
  const [busy, setBusy] = useState(false)
  const [editing, setEditing] = useState<string | null>(null)
  const started = !!job.searched_at
  const scheduled = job.status === 'scheduled'

  async function run(fn: () => Promise<unknown>) {
    setBusy(true)
    try {
      await fn()
      onChanged()
    } catch (e) {
      await dialog.alert({ title: 'Erro', message: (e as Error).message })
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="mt-6 rounded-xl border border-teal-900/60 bg-teal-950/20 p-4">
      <h2 className="mb-2 flex items-center gap-1.5 font-semibold text-teal-300">
        <Calendar width={16} height={16} />
        {scheduled ? (started ? 'Nova busca programada' : 'Busca programada') : 'Busca contínua'}
      </h2>
      {scheduled && (
        <p className="text-sm text-zinc-300">
          {started ? 'Próxima tentativa' : 'Começa a procurar'} em{' '}
          <span className="font-semibold text-teal-200">{fmtWhen(job.next_search_at)}</span>
          {started && job.searched_at && (
            <span className="text-zinc-500"> · última busca {fmtWhen(job.searched_at)}</span>
          )}
        </p>
      )}
      {scheduled && started && (
        <p className="mt-1 text-xs text-zinc-500">
          A nova busca mantém o que já apareceu (se o tracker não responder, a lista não some).
          Enquanto isso, dá para escolher um torrent da lista ou colar um magnet.
        </p>
      )}
      <div className="mt-3 flex flex-wrap items-center gap-2">
        {scheduled && (
          <button onClick={() => run(() => searchNow(job.id))} disabled={busy}
            className="inline-flex items-center gap-1.5 rounded-lg bg-teal-700 px-3 py-1.5 text-sm font-semibold text-white hover:bg-teal-600 disabled:opacity-50">
            <Refresh width={14} height={14} /> Buscar agora
          </button>
        )}
        {scheduled && !started && editing === null && (
          <button onClick={() => setEditing((job.next_search_at ?? '').slice(0, 16))} disabled={busy}
            className="rounded-lg border border-zinc-700 px-3 py-1.5 text-sm text-zinc-300 hover:bg-zinc-800 disabled:opacity-50">
            Mudar data
          </button>
        )}
        {editing !== null && (
          <>
            <input type="datetime-local" value={editing} onChange={(e) => setEditing(e.target.value)}
              className={fieldCls} />
            <button disabled={busy || !editing}
              onClick={() => run(async () => { await rescheduleJob(job.id, scheduleBody(editing)!); setEditing(null) })}
              className="rounded-lg bg-blue-600 px-3 py-1.5 text-sm font-semibold hover:bg-blue-500 disabled:opacity-50">
              Salvar
            </button>
            <button onClick={() => setEditing(null)} disabled={busy}
              className="rounded-lg border border-zinc-700 px-3 py-1.5 text-sm text-zinc-400 hover:bg-zinc-800">
              Cancelar
            </button>
          </>
        )}
        <div className="ml-auto">
          <RetryToggle size="sm" mode={job.retry_mode ?? null} global={global}
            onChange={(m) => run(() => setJobRetryMode(job.id, m ?? 'inherit'))} />
        </div>
      </div>
    </section>
  )
}
