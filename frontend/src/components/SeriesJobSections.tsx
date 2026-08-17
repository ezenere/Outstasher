import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  Check, Download, EditPencil, FastArrowRight, MediaVideo, Refresh,
  SkipNext, SoundHigh, WarningTriangle,
} from 'iconoir-react'
import {
  EPISODE_STATE_LABEL, fmtSize, post, prog, resolveGate,
  seriesTorrentCandidates, switchSeriesTorrent,
  type Candidate, type EpisodeState, type Job, type SeriesCandidate,
  type TorrentChoice,
} from '../api'
import AlignmentReview from './AlignmentReview'
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

/** Torrents do plano (as duas línguas), com barra de progresso, troca manual
 *  ("tentar próximo(s)" / escolher outro) e o botão de forçar a próxima etapa
 *  durante o download. */
export function SeriesTorrents({ job, onChanged }: { job: Job; onChanged: () => void }) {
  const dialog = useDialog()
  const [forcing, setForcing] = useState(false)
  const [switching, setSwitching] = useState(false)
  // lista "Escolher outro…" aberta para qual torrent (+ candidatos buscados)
  const [pickN, setPickN] = useState<number | null>(null)
  const [pickList, setPickList] = useState<SeriesCandidate[] | null>(null)
  const torrents = job.torrents ?? []
  if (!torrents.length) return null

  async function trySwitch(n: number, candidateId: string | null) {
    if (switching) return
    const t = torrents.find((x) => x.n === n)
    if (!(await dialog.confirm({
      title: 'Trocar torrent',
      message: `Trocar ${candidateId ? 'para o candidato selecionado' : 'pelo próximo candidato compatível'}? `
        + `O download atual será descartado e os episódios ${t?.coverage.join(', ')} passam para o novo torrent.`,
      confirmText: 'Trocar', tone: 'danger',
    }))) return
    setSwitching(true)
    try {
      await switchSeriesTorrent(job.id, n, candidateId)
      setPickN(null)
      setPickList(null)
      onChanged()
    } catch (e) {
      await dialog.alert({ title: 'Erro', message: (e as Error).message })
    } finally {
      setSwitching(false)
    }
  }

  async function togglePick(n: number) {
    if (pickN === n) {
      setPickN(null)
      setPickList(null)
      return
    }
    setPickN(n)
    setPickList(null)
    try {
      const r = await seriesTorrentCandidates(job.id, n)
      setPickList(r.candidates)
    } catch (e) {
      setPickN(null)
      await dialog.alert({ title: 'Erro', message: (e as Error).message })
    }
  }

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
              {/* troca manual: como nos filmes, por torrent do plano */}
              {job.status === 'downloading' && t.state === 'downloading' && (
                <div className="mt-1.5">
                  <div className="flex flex-wrap items-center gap-2">
                    <button
                      onClick={() => trySwitch(t.n, null)}
                      disabled={switching}
                      title="Troca pelo próximo candidato que cobre os mesmos episódios"
                      className="inline-flex items-center gap-1 rounded-lg border border-zinc-700 px-2.5 py-1 text-xs text-zinc-300 hover:bg-zinc-800 disabled:opacity-50"
                    >
                      <SkipNext width={13} height={13} /> Tentar próximo(s)
                    </button>
                    <button
                      onClick={() => togglePick(t.n)}
                      className="rounded-lg border border-zinc-700 px-2.5 py-1 text-xs text-zinc-300 hover:bg-zinc-800"
                    >
                      {pickN === t.n ? 'Fechar lista' : 'Escolher outro…'}
                    </button>
                  </div>
                  {pickN === t.n && (
                    <div className="mt-2">
                      <div className="mb-1 text-xs text-zinc-500">
                        Candidatos que cobrem {t.coverage.join(', ')} (a coluna
                        Corte mostra a cobertura). Clique para trocar.
                      </div>
                      {pickList === null ? (
                        <div className="text-xs text-zinc-500">Carregando…</div>
                      ) : (
                        <CandidatesTable
                          candidates={asTableRows(pickList)}
                          selectable
                          onSelect={(cid) => trySwitch(t.n, cid)}
                        />
                      )}
                    </div>
                  )}
                </div>
              )}
            </div>
          )
        })}
      </div>
    </section>
  )
}

/** Relatório final do job de série + recriação de job só com as falhas. */
export function SeriesReport({ job }: { job: Job }) {
  const dialog = useDialog()
  const navigate = useNavigate()
  const [busy, setBusy] = useState(false)
  const report = job.report
  if (!report || !['done', 'error'].includes(job.status)) return null

  async function retryFailed() {
    if (busy || !report) return
    if (!(await dialog.confirm({
      title: 'Baixar de novo as falhas',
      message: `Criar um novo job só com os ${report.failed.length} episódio(s) que falharam (mesmo idioma, destino e opções)?`,
      confirmText: 'Criar job',
    }))) return
    // "S01E03" -> {1: [3]}
    const episodes: Record<number, number[]> = {}
    for (const key of report.failed) {
      const [s, e] = key.slice(1).split('E').map(Number)
      ;(episodes[s] ??= []).push(e)
    }
    setBusy(true)
    try {
      const nj = await post<Job>('/api/jobs/series', {
        tmdb_id: job.tmdb_id,
        language: job.language,
        seasons: [],
        episodes,
        mode: job.mode === 'manual' ? 'manual' : 'auto',
        destination_id: job.destination_id ?? null,
        torrent_target_id: job.torrent_target_id ?? null,
        convert: job.convert ?? null,
      })
      navigate(`/jobs/${nj.id}`)
    } catch (e) {
      await dialog.alert({ title: 'Erro', message: (e as Error).message })
    } finally {
      setBusy(false)
    }
  }

  const ok = report.attempted > 0 && report.succeeded === report.attempted
  return (
    <section className={`mt-6 rounded-xl border p-4 ${
      ok ? 'border-emerald-900/60 bg-emerald-950/20'
         : 'border-amber-900/60 bg-amber-950/20'}`}
    >
      <h2 className={`mb-1 font-semibold ${ok ? 'text-emerald-300' : 'text-amber-300'}`}>
        Relatório
      </h2>
      <p className="text-sm text-zinc-300">
        {report.succeeded}/{report.attempted} episódio(s) concluído(s)
        {report.failed.length > 0 && <> · {report.failed.length} falha(s): <span className="font-mono text-xs">{report.failed.join(', ')}</span></>}
        {(report.skipped?.length ?? 0) > 0 && <> · {report.skipped!.length} pulado(s): <span className="font-mono text-xs">{report.skipped!.join(', ')}</span></>}
      </p>
      {report.failed.length > 0 && (
        <button
          onClick={retryFailed}
          disabled={busy}
          className="mt-3 inline-flex items-center gap-1.5 rounded-lg bg-blue-600 px-4 py-2 text-sm font-semibold hover:bg-blue-500 disabled:opacity-50"
        >
          <Refresh width={15} height={15} /> Baixar de novo os que falharam
        </button>
      )}
    </section>
  )
}

/** Gates de série: lacunas, torrents incompatíveis e escolha manual. */
export function SeriesGate({ job, onResolved }: { job: Job; onResolved: () => void }) {
  const dialog = useDialog()
  const [busy, setBusy] = useState(false)
  // manual invertido: role -> candidate_id -> episódios ('auto' = pelo título)
  const [sel, setSel] = useState<Record<string, Record<string, string[] | 'auto'>>>(
    { original: {}, dubbed: {} })
  // editor de episódios aberto para qual (role, candidato)
  const [editing, setEditing] = useState<{ role: string; cid: string } | null>(null)
  const gate = job.awaiting
  if (job.status !== 'awaiting' || !gate) return null

  if (gate.reason === 'alignment_review') {
    return <AlignmentReview job={job} onResolved={onResolved} />
  }

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
            onClick={() => send('gaps_confirm', { edit: true })}
            disabled={busy}
            title="Abre a escolha manual para cobrir os episódios que faltam com outros torrents"
            className="inline-flex items-center gap-1.5 rounded-lg border border-zinc-700 px-4 py-2 text-sm text-zinc-300 hover:bg-zinc-800 disabled:opacity-50"
          >
            <EditPencil width={14} height={14} /> Editar seleção manual
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
    const byTorrent = gate.payload.by_torrent ?? { original: [], dubbed: [] }
    const requested = gate.payload.requested ?? []

    // episódios cobertos por papel com a seleção atual (auto = matches)
    function coveredBy(role: string): Set<string> {
      const out = new Set<string>()
      for (const [cid, eps] of Object.entries(sel[role] ?? {})) {
        if (eps === 'auto') {
          const c = byTorrent[role as 'original' | 'dubbed']?.find((x) => x.id === cid)
          c?.matches.forEach((k) => out.add(k))
        } else {
          eps.forEach((k) => out.add(k))
        }
      }
      return out
    }

    function toggleTorrent(role: string, cid: string) {
      setSel((cur) => {
        const next = { ...cur, [role]: { ...(cur[role] ?? {}) } }
        if (cid in next[role]) delete next[role][cid]
        else next[role][cid] = 'auto'
        return next
      })
      setEditing(null)
    }

    function toggleEp(role: string, cid: string, key: string, matches: string[]) {
      setSel((cur) => {
        const cboth = cur[role]?.[cid]
        // 'auto' vira lista explícita (partindo dos matches) na 1ª edição
        const base = new Set(cboth === 'auto' || cboth === undefined ? matches : cboth)
        if (base.has(key)) base.delete(key)
        else base.add(key)
        return { ...cur, [role]: { ...(cur[role] ?? {}), [cid]: [...base].sort() } }
      })
    }

    const anySelected = Object.values(sel).some((m) => Object.keys(m).length > 0)

    function submit() {
      const torrents: { candidate_id: string; role: string; episodes: string[] | 'auto' }[] = []
      for (const role of ['original', 'dubbed'] as const) {
        for (const [cid, eps] of Object.entries(sel[role] ?? {})) {
          torrents.push({ candidate_id: cid, role, episodes: eps })
        }
      }
      void send('manual_pick', { torrents })
    }

    return (
      <section className="mt-6 rounded-xl border border-purple-900/60 bg-purple-950/20 p-4">
        <h2 className="mb-2 font-semibold text-purple-300">Escolha os torrents</h2>
        <p className="mb-3 text-sm text-zinc-300">
          Marque os torrents que quer baixar; cada um assume automaticamente os
          episódios que o TÍTULO indica (dá para editar). Papel sem nenhuma
          marca mantém o plano automático. O que ficar sem cobertura passa pelo
          aviso de lacunas antes de baixar; o match fino com os ARQUIVOS
          acontece depois do download.
        </p>
        {(['original', 'dubbed'] as const).map((role) => {
          const covered = coveredBy(role)
          const touched = Object.keys(sel[role] ?? {}).length > 0
          const missing = requested.filter((k) => !covered.has(k))
          return (
            <div key={role} className="mb-4">
              <h3 className="mb-1.5 flex items-center gap-1.5 text-sm text-zinc-400">
                {role === 'original'
                  ? <><MediaVideo width={14} height={14} /> Original (vídeo)</>
                  : <><SoundHigh width={14} height={14} /> Dublado ({job.language})</>}
                {touched && (
                  <span className={`text-xs ${missing.length ? 'text-amber-300' : 'text-emerald-400'}`}>
                    {missing.length
                      ? `faltando: ${missing.join(', ')}`
                      : 'todos os episódios cobertos'}
                  </span>
                )}
                {!touched && <span className="text-xs text-zinc-500">(plano automático)</span>}
              </h3>
              <div className="max-h-80 space-y-1 overflow-y-auto pr-1">
                {(byTorrent[role] ?? []).map((c) => {
                  const checked = c.id in (sel[role] ?? {})
                  const eps = sel[role]?.[c.id]
                  const isEditing = editing?.role === role && editing.cid === c.id
                  return (
                    <div key={c.id} className={`rounded-lg border px-2.5 py-1.5 ${
                      checked ? 'border-purple-700 bg-purple-950/30' : 'border-zinc-800'}`}
                    >
                      <label className="flex cursor-pointer items-center gap-2 text-sm">
                        <input type="checkbox" checked={checked}
                          onChange={() => toggleTorrent(role, c.id)} />
                        <span className="min-w-0 flex-1 truncate" title={c.title}>{c.title}</span>
                        <span className="shrink-0 text-xs text-zinc-500">
                          {c.quality ?? '—'} · {fmtSize(c.size)} · {c.seeders} seeds
                        </span>
                        <span className="shrink-0 rounded bg-zinc-800 px-1.5 py-0.5 text-xs text-zinc-300"
                          title="O que o título do torrent indica">
                          {c.coverage ?? '?'}
                        </span>
                      </label>
                      {checked && (
                        <div className="mt-1 flex flex-wrap items-center gap-1.5 pl-6 text-xs">
                          <span className="text-zinc-400">
                            {eps === 'auto'
                              ? `auto: ${c.matches.length ? c.matches.join(', ') : 'nenhum match pelo título!'}`
                              : `episódios: ${(eps as string[]).join(', ') || 'nenhum'}`}
                          </span>
                          {eps === 'auto' && c.matches.length === 0 && (
                            <span className="text-amber-300">— atribua manualmente</span>
                          )}
                          <button
                            onClick={() => setEditing(isEditing ? null : { role, cid: c.id })}
                            className="rounded border border-zinc-700 px-1.5 py-0.5 text-zinc-300 hover:bg-zinc-800"
                          >
                            {isEditing ? 'fechar' : 'editar episódios'}
                          </button>
                          {isEditing && (
                            <div className="mt-1 flex w-full flex-wrap gap-1 pl-1">
                              {requested.map((k) => {
                                const active = eps === 'auto'
                                  ? c.matches.includes(k)
                                  : (eps as string[]).includes(k)
                                const offTitle = !c.matches.includes(k)
                                return (
                                  <button
                                    key={k}
                                    onClick={() => toggleEp(role, c.id, k, c.matches)}
                                    title={offTitle
                                      ? 'O título do torrent não indica este episódio — atribuir mesmo assim é decisão sua'
                                      : undefined}
                                    className={`rounded px-1.5 py-0.5 font-mono text-[11px] ${
                                      active
                                        ? offTitle
                                          ? 'bg-amber-600 font-semibold text-zinc-950'
                                          : 'bg-purple-600 font-semibold text-white'
                                        : 'bg-zinc-800 text-zinc-400 hover:bg-zinc-700'
                                    }`}
                                  >
                                    {k}
                                  </button>
                                )
                              })}
                            </div>
                          )}
                        </div>
                      )}
                    </div>
                  )
                })}
                {(byTorrent[role] ?? []).length === 0 && (
                  <div className="text-xs text-zinc-500">Nenhum candidato encontrado para este papel.</div>
                )}
              </div>
            </div>
          )
        })}
        <button
          onClick={submit}
          disabled={busy}
          className="mt-1 inline-flex items-center gap-1.5 rounded-lg bg-blue-600 px-4 py-2 text-sm font-semibold hover:bg-blue-500 disabled:opacity-50"
        >
          <Download width={15} height={15} />
          {anySelected ? 'Confirmar seleção' : 'Confirmar plano automático'}
        </button>
      </section>
    )
  }

  return null
}
