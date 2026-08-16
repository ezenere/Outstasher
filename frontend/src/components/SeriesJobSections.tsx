import { useState } from 'react'
import {
  Check, Download, FastArrowRight, MediaVideo, SoundHigh, WarningTriangle,
} from 'iconoir-react'
import {
  EPISODE_STATE_LABEL, fmtSize, prog, resolveGate,
  type Candidate, type EpisodeState, type Job, type SeriesCandidate,
} from '../api'
import { useDialog } from './Dialog'
import { CandidatesTable, Collapsible, ProgressBar } from './ui'

/** Candidato de série na CandidatesTable: a coluna "Corte" (sem sentido em
 *  série) exibe a COBERTURA do torrent (S01E02 / S01 completa / série
 *  completa) — é o dado que decide a escolha de um pack. */
function asTableRows(cands: SeriesCandidate[]): Candidate[] {
  return cands.map((c) => ({
    ...c, edition: c.coverage, rejected: null, chosen: false,
  })) as unknown as Candidate[]
}

const STATE_TONE: Partial<Record<EpisodeState, string>> = {
  downloading: 'bg-yellow-950 text-yellow-300',
  downloaded: 'bg-sky-950 text-sky-300',
  merging: 'bg-purple-950 text-purple-300',
  done: 'bg-emerald-950 text-emerald-300',
  failed: 'bg-red-950 text-red-300',
  review: 'bg-amber-950 text-amber-300',
  skipped_future: 'bg-zinc-800 text-zinc-400',
  skipped_missing: 'bg-zinc-800 text-zinc-400',
}

function EpisodeBadge({ state }: { state: EpisodeState }) {
  return (
    <span className={`rounded px-1.5 py-0.5 text-xs font-medium ${
      STATE_TONE[state] ?? 'bg-zinc-800 text-zinc-300'}`}
    >
      {EPISODE_STATE_LABEL[state] ?? state}
    </span>
  )
}

/** Tabela de episódios do job de série (estado por episódio). */
export function SeriesEpisodes({ job }: { job: Job }) {
  const eps = Object.entries(job.episodes ?? {})
  if (!eps.length) return null
  return (
    <section className="mt-6">
      <h2 className="mb-2 text-sm font-semibold text-zinc-400">
        Episódios ({eps.length})
      </h2>
      <div className="max-h-96 overflow-auto rounded-xl border border-zinc-800">
        <table className="w-full border-collapse text-sm">
          <thead>
            <tr className="sticky top-0 bg-zinc-900 text-left text-xs text-zinc-400">
              <th className="px-3 py-1.5">Episódio</th>
              <th className="px-3 py-1.5">Nome</th>
              <th className="px-3 py-1.5">Estreia</th>
              <th className="px-3 py-1.5">Estado</th>
            </tr>
          </thead>
          <tbody>
            {eps.map(([key, ep]) => (
              <tr key={key} className="border-t border-zinc-800/60">
                <td className="px-3 py-1.5 font-mono text-xs">{key}</td>
                <td className="max-w-72 truncate px-3 py-1.5 text-zinc-300" title={ep.name ?? undefined}>
                  {ep.name ?? '—'}
                </td>
                <td className="px-3 py-1.5 text-xs text-zinc-500 tabular-nums">{ep.air_date ?? '—'}</td>
                <td className="px-3 py-1.5">
                  <EpisodeBadge state={ep.state} />
                  {ep.error && (
                    <span className="ml-2 text-xs text-red-400" title={ep.error}>
                      {ep.error}
                    </span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  )
}

/** Torrents do plano (as duas línguas), com barra de progresso e o botão de
 *  forçar a próxima etapa durante o download. */
export function SeriesTorrents({ job, onChanged }: { job: Job; onChanged: () => void }) {
  const dialog = useDialog()
  const [forcing, setForcing] = useState(false)
  const torrents = job.torrents ?? []
  if (!torrents.length) return null

  async function forceNext() {
    if (forcing) return
    if (!(await dialog.confirm({
      title: 'Forçar próxima etapa',
      message: 'Os torrents que ainda não terminaram serão abandonados e os episódios que dependem só deles serão PULADOS. Continuar?',
      confirmText: 'Forçar', tone: 'danger',
    }))) return
    setForcing(true)
    try {
      await resolveGate(job.id, 'force_continue')
      onChanged()
    } catch (e) {
      await dialog.alert({ title: 'Erro', message: (e as Error).message })
    } finally {
      setForcing(false)
    }
  }

  return (
    <section className="mt-6">
      <div className="mb-2 flex items-center gap-3">
        <h2 className="text-sm font-semibold text-zinc-400">
          Torrents ({torrents.filter((t) => t.state === 'done').length}/{torrents.length} concluídos)
        </h2>
        {job.status === 'downloading' && (
          <button
            onClick={forceNext}
            disabled={forcing}
            title="Segue para a próxima etapa sem esperar os downloads que faltam"
            className="inline-flex items-center gap-1 rounded-lg border border-amber-800/60 px-2.5 py-1 text-xs text-amber-300 hover:bg-amber-950/40 disabled:opacity-50"
          >
            <FastArrowRight width={13} height={13} /> Forçar próxima etapa
          </button>
        )}
      </div>
      <div className="flex flex-col gap-2">
        {torrents.map((t) => {
          const p = prog(t.progress)
          return (
            <div key={t.n} className="rounded-xl border border-zinc-800 px-3 py-2">
              <div className="flex flex-wrap items-center gap-2 text-sm">
                <span className={`flex items-center gap-1 rounded px-1.5 py-0.5 text-xs font-medium ${
                  t.role === 'original' ? 'bg-sky-950 text-sky-300' : 'bg-purple-950 text-purple-300'}`}
                >
                  {t.role === 'original'
                    ? <><MediaVideo width={11} height={11} /> original</>
                    : <><SoundHigh width={11} height={11} /> dublado</>}
                </span>
                <span className="min-w-0 flex-1 truncate" title={t.title}>{t.title}</span>
                {t.state === 'done' && <Check width={15} height={15} className="shrink-0 text-emerald-400" />}
                {t.state === 'abandoned' && (
                  <span className="shrink-0 text-xs text-zinc-500">abandonado</span>
                )}
              </div>
              <div className="mt-0.5 flex flex-wrap gap-x-2 text-xs text-zinc-500">
                <span>{t.coverage.length} episódio(s): {t.coverage.join(', ')}</span>
                {t.quality && <span>· {t.quality}</span>}
                {t.size != null && <span>· {fmtSize(t.size)}</span>}
                {t.selected_files && t.selected_files.length > 0 && (
                  <span title={t.selected_files.join('\n')}>
                    · {t.selected_files.length} arquivo(s) selecionado(s) do pack
                  </span>
                )}
              </div>
              {t.state !== 'done' && p && <ProgressBar label="Download" p={p} />}
            </div>
          )
        })}
      </div>
    </section>
  )
}

/** Gates de série: lacunas, torrents incompatíveis e escolha manual. */
export function SeriesGate({ job, onResolved }: { job: Job; onResolved: () => void }) {
  const dialog = useDialog()
  const [busy, setBusy] = useState(false)
  // escolhas do manual_pick: ep_key -> role -> candidate_id
  const [picks, setPicks] = useState<Record<string, Record<string, string>>>({})
  const gate = job.awaiting
  if (job.status !== 'awaiting' || !gate) return null

  async function send(reason: Parameters<typeof resolveGate>[1], decision: object,
                      confirm?: { message: string; tone?: 'danger' }) {
    if (busy) return
    if (confirm && !(await dialog.confirm({
      title: 'Confirmar', message: confirm.message,
      confirmText: 'Confirmar', tone: confirm.tone,
    }))) return
    setBusy(true)
    try {
      await resolveGate(job.id, reason, decision)
      onResolved()
    } catch (e) {
      await dialog.alert({ title: 'Erro', message: (e as Error).message })
    } finally {
      setBusy(false)
    }
  }

  if (gate.reason === 'gaps_confirm') {
    const missing = gate.payload.missing ?? []
    return (
      <section className="mt-6 rounded-xl border border-amber-900/60 bg-amber-950/20 p-4">
        <h2 className="mb-2 flex items-center gap-1.5 font-semibold text-amber-300">
          <WarningTriangle width={16} height={16} /> Episódios sem torrent
        </h2>
        <p className="text-sm text-zinc-300">
          Não encontrei torrent para todos os episódios pedidos. Se continuar,
          os episódios abaixo ficam de fora (dá para buscá-los de novo depois
          num novo job).
        </p>
        <ul className="mt-2 space-y-1 text-sm">
          {missing.map((m) => (
            <li key={m.episode} className="flex flex-wrap items-center gap-2">
              <span className="font-mono text-xs">{m.episode}</span>
              <span className="min-w-0 truncate text-zinc-400">{m.name ?? ''}</span>
              <span className="text-xs text-amber-300">falta: {m.missing.join(' + ')}</span>
            </li>
          ))}
        </ul>
        <div className="mt-3 flex flex-wrap gap-2">
          <button
            onClick={() => send('gaps_confirm', { accept: true })}
            disabled={busy}
            className="inline-flex items-center gap-1.5 rounded-lg bg-amber-600 px-4 py-2 text-sm font-semibold text-zinc-950 hover:bg-amber-500 disabled:opacity-50"
          >
            Continuar com as lacunas
          </button>
          <button
            onClick={() => send('gaps_confirm', { accept: false },
              { message: 'Cancelar o job inteiro por causa das lacunas?', tone: 'danger' })}
            disabled={busy}
            className="rounded-lg border border-zinc-700 px-4 py-2 text-sm text-zinc-300 hover:bg-zinc-800 disabled:opacity-50"
          >
            Cancelar o job
          </button>
        </div>
      </section>
    )
  }

  if (gate.reason === 'incompatible_torrents') {
    const items = gate.payload.torrents ?? []
    const groups = gate.payload.episode_groups ?? []
    return (
      <section className="mt-6 rounded-xl border border-amber-900/60 bg-amber-950/20 p-4">
        <h2 className="mb-2 flex items-center gap-1.5 font-semibold text-amber-300">
          <WarningTriangle width={16} height={16} /> Torrents possivelmente incompatíveis
        </h2>
        <p className="text-sm text-zinc-300">
          Os torrents abaixo têm sinais de que a ordem/divisão de episódios
          pode não corresponder à do TMDB. Nada será baixado sem a sua decisão.
        </p>
        <div className="mt-3 space-y-3">
          {items.map((it) => (
            <div key={it.n} className="rounded-lg border border-zinc-800 bg-zinc-900/40 p-3">
              <div className="flex flex-wrap items-center gap-2 text-sm">
                <span className="text-xs text-zinc-500">t{it.n} · {it.role === 'original' ? 'original' : 'dublado'}</span>
                <span className="min-w-0 flex-1 truncate font-medium" title={it.title}>{it.title}</span>
              </div>
              <ul className="mt-1.5 list-inside list-disc text-xs text-amber-200/90">
                {it.signals.map((s, i) => <li key={i}>{s}</li>)}
              </ul>
              {it.alternatives.length > 0 && (
                <Collapsible title={`Trocar por outro candidato (${it.alternatives.length})`}>
                  <CandidatesTable
                    candidates={asTableRows(it.alternatives)}
                    selectable
                    onSelect={(cid) => send('incompatible_torrents',
                      { action: 'replace', torrent_n: it.n, candidate_id: cid },
                      { message: 'Trocar este torrent pelo candidato selecionado?' })}
                  />
                </Collapsible>
              )}
            </div>
          ))}
        </div>
        {groups.length > 0 && (
          <div className="mt-3 rounded-lg border border-zinc-800 bg-zinc-900/40 p-3 text-sm">
            <div className="mb-1 text-zinc-300">
              O TMDB conhece ordens alternativas desta série — se os releases
              usam outra numeração (DVD/absoluta), remapeie os episódios:
            </div>
            <div className="flex flex-wrap gap-2">
              {groups.map((g) => (
                <button
                  key={g.id}
                  onClick={() => send('incompatible_torrents',
                    { action: 'remap', group_id: g.id },
                    { message: `Remapear os episódios pedidos para a ordem "${g.name}" e refazer busca e plano?`, tone: 'danger' })}
                  disabled={busy}
                  className="rounded-lg border border-zinc-700 px-3 py-1.5 text-xs text-zinc-300 hover:bg-zinc-800 disabled:opacity-50"
                >
                  {g.name} ({g.episode_count} ep.)
                </button>
              ))}
            </div>
          </div>
        )}
        <button
          onClick={() => send('incompatible_torrents', { action: 'accept' })}
          disabled={busy}
          className="mt-3 inline-flex items-center gap-1.5 rounded-lg bg-amber-600 px-4 py-2 text-sm font-semibold text-zinc-950 hover:bg-amber-500 disabled:opacity-50"
        >
          Aceitar mesmo assim e baixar
        </button>
      </section>
    )
  }

  if (gate.reason === 'manual_pick') {
    const cands = gate.payload.candidates ?? { original: {}, dubbed: {} }
    const keys = Object.keys(job.episodes ?? {}).filter(
      (k) => !['skipped_future', 'skipped_missing'].includes(job.episodes![k].state))
    return (
      <section className="mt-6 rounded-xl border border-purple-900/60 bg-purple-950/20 p-4">
        <h2 className="mb-2 font-semibold text-purple-300">Escolha os torrents por episódio</h2>
        <p className="mb-3 text-sm text-zinc-300">
          Episódio sem escolha explícita usa o melhor candidato automático.
          Escolher o mesmo pack para vários episódios baixa o pack uma vez só.
        </p>
        <div className="space-y-2">
          {keys.map((key) => (
            <Collapsible
              key={key}
              title={`${key} — ${job.episodes![key].name ?? ''}`}
              right={picks[key] ? <span className="text-xs text-purple-300">escolhido</span> : undefined}
            >
              {(['original', 'dubbed'] as const).map((role) => (
                <div key={role} className="mb-2">
                  <h4 className="mb-1 flex items-center gap-1.5 text-xs text-zinc-400">
                    {role === 'original'
                      ? <><MediaVideo width={12} height={12} /> Original (vídeo)</>
                      : <><SoundHigh width={12} height={12} /> Dublado ({job.language})</>}
                  </h4>
                  <CandidatesTable
                    candidates={asTableRows(cands[role]?.[key] ?? [])}
                    selectable
                    selectedId={picks[key]?.[role]}
                    onSelect={(cid) => setPicks((cur) => ({
                      ...cur, [key]: { ...(cur[key] ?? {}), [role]: cid },
                    }))}
                  />
                </div>
              ))}
            </Collapsible>
          ))}
        </div>
        <button
          onClick={() => send('manual_pick', { picks })}
          disabled={busy}
          className="mt-3 inline-flex items-center gap-1.5 rounded-lg bg-blue-600 px-4 py-2 text-sm font-semibold hover:bg-blue-500 disabled:opacity-50"
        >
          <Download width={15} height={15} />
          {Object.keys(picks).length
            ? `Confirmar (${Object.keys(picks).length} escolha(s) manual(is))`
            : 'Confirmar plano automático'}
        </button>
      </section>
    )
  }

  return null
}
