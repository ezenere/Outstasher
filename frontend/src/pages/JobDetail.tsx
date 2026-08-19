import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import {
  Check, Download, Folder, Magnet, MediaVideo, NavArrowLeft, Play, Refresh,
  Settings as SettingsIcon, SkipNext, SoundHigh, Trash, WarningTriangle,
} from 'iconoir-react'
import { convertSummary, post, prog, type Job, type JobEvent, type JobProgress } from '../api'
import { api } from '../api'
import { Badge, CandidatesTable, ClampText, Collapsible, Elapsed, Empty, KindTags, MergeBar, ProgressBar, alignRole } from '../components/ui'
import { useDialog } from '../components/Dialog'
import { SeriesEpisodes, SeriesGate, SeriesReport, SeriesTorrents } from '../components/SeriesJobSections'
import AlignmentReview from '../components/AlignmentReview'
import { jobTitle, removeJob } from './Jobs'

export default function JobDetail() {
  const { id } = useParams<{ id: string }>()
  const dialog = useDialog()
  const [job, setJob] = useState<Job | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [selAudio, setSelAudio] = useState<string | undefined>()
  const [selVideo, setSelVideo] = useState<string | undefined>()
  const [submitting, setSubmitting] = useState(false)
  // troca de torrent durante o download: qual lista está aberta + trava anti-duplo-clique
  const [pickKind, setPickKind] = useState<'video' | 'audio' | null>(null)
  const [switching, setSwitching] = useState(false)
  // magnet/link próprio para trocar o torrent em andamento (por papel)
  const [magnetForm, setMagnetForm] = useState<
    { kind: 'video' | 'audio'; url: string; title: string } | null>(null)
  // timeline completa fica escondida por padrão (só os eventos recentes à vista)
  const [fullTimeline, setFullTimeline] = useState(false)
  const navigate = useNavigate()

  // detalhe completo (eventos, candidatos, destinos): recarrega a cada 5s
  const reload = useCallback(async () => {
    if (!id) return
    try {
      setJob(await api<Job>(`/api/jobs/${id}`))
      setError(null)
    } catch (e) {
      setError((e as Error).message)
    }
  }, [id])

  useEffect(() => {
    void reload()
    const t = setInterval(reload, 5000)
    return () => clearInterval(t)
  }, [reload])

  // progresso (download/conversão): tick rápido de 1s, sem puxar os eventos.
  // Só ativo quando há progresso mudando (baixando/convertendo); em outros
  // estados o reload de 5s já basta. Só remenda status/detail/progress/output.
  const live = job?.status === 'downloading' || job?.status === 'merging'
  useEffect(() => {
    if (!id || !live) return
    let stop = false
    async function tick() {
      try {
        const p = await api<JobProgress>(`/api/jobs/${id}/progress`)
        if (stop) return
        setJob((cur) =>
          cur ? { ...cur, status: p.status, detail: p.detail, progress: p.progress,
                  output: p.output, merge_started_at: p.merge_started_at } : cur,
        )
      } catch {
        /* 404 quando o job some / servidor reiniciando: o reload de 5s trata */
      }
    }
    const t = setInterval(tick, 1000)
    return () => {
      stop = true
      clearInterval(t)
    }
  }, [id, live])

  // pre-seleciona os melhores quando o job esta aguardando escolha
  useEffect(() => {
    if (job?.status === 'awaiting' && job.search) {
      setSelAudio((cur) => cur ?? job.search!.audio[0]?.id)
      setSelVideo((cur) => cur ?? job.search!.video[0]?.id)
    }
  }, [job?.status, job?.search])

  const allEvents = useMemo(() => job?.events ?? [], [job?.events])
  const candEvents = useMemo(
    () => allEvents.filter((e) => e.kind === 'candidates' && e.data?.candidates),
    [allEvents],
  )
  const timeline = useMemo(() => allEvents.filter((e) => e.kind !== 'candidates'), [allEvents])
  // "eventos recentes": da última transição de status (bolinha azul) para baixo.
  // É o que importa agora; a timeline anterior fica atrás do "ver completa".
  const recent = useMemo(() => {
    const lastStatus = timeline.map((e) => e.kind).lastIndexOf('status')
    return lastStatus <= 0 ? timeline : timeline.slice(lastStatus)
  }, [timeline])

  if (error) return <Empty>Erro: {error}</Empty>
  if (!job) return <Empty>Carregando...</Empty>

  const pv = prog(job.progress?.video)
  const pa = prog(job.progress?.audio)
  const movie = job.movie

  // que torrents este job baixa (kind = both | original | dubbed)
  const needAudio = job.kind !== 'original'
  const needVideo = job.kind !== 'dubbed'
  const selectionReady = (!needAudio || !!selAudio) && (!needVideo || !!selVideo)

  async function submitSelection() {
    if (!job || !selectionReady) return
    const a = needAudio ? job.search?.audio.find((c) => c.id === selAudio) : null
    const v = needVideo ? job.search?.video.find((c) => c.id === selVideo) : null
    // aviso de corte só quando os dois são baixados (merge)
    if (a && v) {
      const ea = a.edition ?? null
      const ev = v.edition ?? null
      if (ea !== ev && !(await dialog.confirm({
        title: 'Cortes diferentes',
        message: `Áudio e vídeo têm cortes diferentes (${ea ?? 'normal'} ≠ ${ev ?? 'normal'}) — os áudios provavelmente não vão alinhar. Continuar mesmo assim?`,
        confirmText: 'Continuar', tone: 'danger',
      }))) return
    }
    // já houve uma escolha antes (pausa de drift, troca de versão): o torrent
    // anterior de cada papel trocado sai do qBittorrent, com os dados
    const trocados = [
      a && job.current?.audio && job.current.audio.id !== a.id ? 'áudio' : null,
      v && job.current?.video && job.current.video.id !== v.id ? 'vídeo' : null,
    ].filter(Boolean)
    if (trocados.length && !(await dialog.confirm({
      title: 'Descartar o download anterior',
      message: `O torrent atual de ${trocados.join(' e ')} será removido do qBittorrent (com os arquivos baixados) e substituído pelo escolhido. Continuar?`,
      confirmText: 'Continuar', tone: 'danger',
    }))) return
    setSubmitting(true)
    try {
      await post(`/api/jobs/${job.id}/select`, {
        audio_id: needAudio ? selAudio : null,
        video_id: needVideo ? selVideo : null,
      })
      void reload()
    } catch (e) {
      await dialog.alert({ title: 'Erro', message: (e as Error).message })
    } finally {
      setSubmitting(false)
    }
  }

  // pausa de drift: converte mesmo com offsets divergentes (possível outra
  // versão) ou roda o alinhamento avançado (EDL por conteúdo, o das séries)
  async function proceedAnyway(mode: 'offset' | 'advanced' = 'offset') {
    if (!job || submitting) return
    setSubmitting(true)
    try {
      await post(`/api/jobs/${job.id}/proceed`, { mode })
      void reload()
    } catch (e) {
      await dialog.alert({ title: 'Erro', message: (e as Error).message })
    } finally {
      setSubmitting(false)
    }
  }

  // troca o torrent em andamento: sem candidateId = "Tentar próximo" (reserva)
  async function trySwitch(kind: 'video' | 'audio', candidateId?: string) {
    if (!job || switching) return
    const what = candidateId ? 'o torrent selecionado' : 'o próximo candidato reserva'
    // em jobs de merge, avisa se o corte escolhido não bate com o do outro torrent
    let warn = ''
    let mismatch = false
    if (candidateId && job.kind !== 'original' && job.kind !== 'dubbed') {
      const cand = (kind === 'video' ? job.search?.video : job.search?.audio)?.find((c) => c.id === candidateId)
      const other = kind === 'video' ? job.audio_torrent : job.video_torrent
      if (cand && other && (cand.edition ?? null) !== (other.edition ?? null)) {
        mismatch = true
        warn = ` Atenção: corte diferente do outro torrent (${cand.edition ?? 'normal'} ≠ ${other.edition ?? 'normal'}) — os áudios podem não alinhar.`
      }
    }
    if (!(await dialog.confirm({
      title: 'Trocar torrent',
      message: `Trocar para ${what}? O download atual de ${kind === 'video' ? 'vídeo' : 'áudio'} será descartado.${warn}`,
      confirmText: 'Trocar', tone: mismatch ? 'danger' : 'default',
    }))) return
    setSwitching(true)
    try {
      await post(`/api/jobs/${job.id}/switch`, { kind, candidate_id: candidateId ?? null })
      setPickKind(null)
      void reload()
    } catch (e) {
      await dialog.alert({ title: 'Erro', message: (e as Error).message })
    } finally {
      setSwitching(false)
    }
  }

  // troca por um magnet/link informado à mão (não passou pelo indexador)
  async function switchToMagnet() {
    if (!job || !magnetForm || switching) return
    const url = magnetForm.url.trim()
    if (!url) {
      await dialog.alert({ title: 'Faltou o link', message: 'Informe um magnet: ou link de .torrent.' })
      return
    }
    const alvo = magnetForm.kind === 'video' ? 'vídeo' : 'áudio'
    if (!(await dialog.confirm({
      title: 'Trocar torrent',
      message: `Trocar o download de ${alvo} pelo torrent informado? O download atual será descartado.`,
      confirmText: 'Trocar',
    }))) return
    setSwitching(true)
    try {
      await post(`/api/jobs/${job.id}/switch`, {
        kind: magnetForm.kind,
        custom: { url, title: magnetForm.title.trim() },
      })
      setMagnetForm(null)
      setPickKind(null)
      void reload()
    } catch (e) {
      await dialog.alert({ title: 'Erro', message: (e as Error).message })
    } finally {
      setSwitching(false)
    }
  }

  // controles de troca exibidos sob a barra de progresso enquanto baixa
  function switchControls(kind: 'video' | 'audio') {
    if (job?.status !== 'downloading') return null
    const list = kind === 'video' ? job.search?.video : job.search?.audio
    const current = kind === 'video' ? job.video_torrent : job.audio_torrent
    return (
      <div className="mt-1.5">
        <div className="flex flex-wrap items-center gap-2">
          <button
            onClick={() => trySwitch(kind)}
            disabled={switching}
            title="Troca pelo próximo candidato reserva"
            className="inline-flex items-center gap-1 rounded-lg border border-zinc-700 px-2.5 py-1 text-xs text-zinc-300 hover:bg-zinc-800 disabled:opacity-50"
          >
            <SkipNext width={13} height={13} /> Próximo
          </button>
          {(list?.length ?? 0) > 0 && (
            <button
              onClick={() => setPickKind(pickKind === kind ? null : kind)}
              className="rounded-lg border border-zinc-700 px-2.5 py-1 text-xs text-zinc-300 hover:bg-zinc-800"
            >
              {pickKind === kind ? 'Fechar lista' : 'Escolher outro…'}
            </button>
          )}
          <button
            onClick={() => setMagnetForm(
              magnetForm?.kind === kind ? null : { kind, url: '', title: '' })}
            className="rounded-lg border border-dashed border-zinc-700 px-2.5 py-1 text-xs text-zinc-400 hover:border-zinc-500 hover:text-zinc-200"
          >
            {magnetForm?.kind === kind ? 'Cancelar' : '+ Magnet/link próprio'}
          </button>
        </div>
        {magnetForm?.kind === kind && (
          <div className="mt-2 rounded-lg border border-zinc-700 bg-zinc-900 p-2.5">
            <div className="grid gap-2 sm:grid-cols-[1fr_1fr_auto]">
              <input
                value={magnetForm.url}
                onChange={(e) => setMagnetForm({ ...magnetForm, url: e.target.value })}
                placeholder="magnet:?xt=… ou https://…/arquivo.torrent"
                className="rounded-lg border border-zinc-700 bg-zinc-800 px-2 py-1.5 font-mono text-xs outline-none focus:border-blue-500"
              />
              <input
                value={magnetForm.title}
                onChange={(e) => setMagnetForm({ ...magnetForm, title: e.target.value })}
                placeholder="Título (opcional — sai do dn= do magnet; define o corte mostrado)"
                className="rounded-lg border border-zinc-700 bg-zinc-800 px-2 py-1.5 text-xs outline-none focus:border-blue-500"
              />
              <button
                onClick={switchToMagnet}
                disabled={switching}
                className="rounded-lg bg-blue-600 px-3 py-1.5 text-xs font-semibold hover:bg-blue-500 disabled:opacity-50"
              >
                Trocar
              </button>
            </div>
            <div className="mt-1.5 text-xs text-zinc-500">
              O torrent entra direto no qBittorrent, sem passar pelo indexador — o
              corte não é conferido contra o outro arquivo.
            </div>
          </div>
        )}
        {pickKind === kind && list && (
          <div className="mt-2">
            <div className="mb-1 text-xs text-zinc-500">
              O torrent atual está destacado. Clique em outro para trocar.
            </div>
            <CandidatesTable candidates={list} selectable current={current} onSelect={(cid) => trySwitch(kind, cid)} />
          </div>
        )}
      </div>
    )
  }

  return (
    <div>
      <div className="flex flex-wrap items-center gap-3">
        <Link to="/jobs" className="rounded-lg border border-zinc-700 p-1.5 text-zinc-400 hover:text-zinc-200">
          <NavArrowLeft width={16} height={16} />
        </Link>
        <h1 className="flex-1 text-lg font-semibold">{jobTitle(job)}</h1>
        <Badge status={job.status} />
        {(job.status === 'error' || job.status === 'cancelled') && (
          <button
            onClick={() => post(`/api/jobs/${job.id}/retry`)
              .then(() => navigate('/jobs'))
              .catch((e) => dialog.alert({ title: 'Erro', message: (e as Error).message }))}
            title="Tentar de novo"
            className="rounded-lg border border-zinc-700 p-1.5 text-zinc-400 hover:text-zinc-200"
          >
            <Refresh width={15} height={15} />
          </button>
        )}
        <button
          onClick={() => removeJob(dialog, job.id, () => navigate('/jobs'))}
          title="Remover job"
          className="rounded-lg border border-zinc-700 p-1.5 text-zinc-400 hover:text-zinc-200"
        >
          <Trash width={15} height={15} />
        </button>
      </div>

      <div className="mt-2 flex flex-wrap items-center gap-1">
        <KindTags kind={job.kind} language={job.language} downloadOnly={job.download_only}
          convert={job.convert} mode={job.mode} />
      </div>

      <ClampText className="mt-2 text-sm text-zinc-400">{job.detail}</ClampText>

      {/* capa + sinopse do filme */}
      {movie && (movie.poster || movie.overview) && (
        <section className="mt-5 flex gap-4 rounded-xl border border-zinc-800 bg-zinc-900/40 p-4">
          {movie.poster && (
            <img
              src={movie.poster}
              alt=""
              className="h-40 w-auto shrink-0 rounded-lg bg-zinc-800 object-cover"
            />
          )}
          <div className="min-w-0">
            <div className="font-semibold">
              {movie.original_title}
              {movie.year ? <span className="font-normal text-zinc-400"> ({movie.year})</span> : ''}
            </div>
            {movie.localized_title && movie.localized_title !== movie.original_title && (
              <div className="text-sm text-zinc-400">{movie.localized_title}</div>
            )}
            {movie.overview && (
              <p className="mt-2 text-sm leading-relaxed text-zinc-300">{movie.overview}</p>
            )}
          </div>
        </section>
      )}

      {/* ---- ações pendentes (o mais urgente vem primeiro) ---- */}
      {/* gates de série (lacunas / incompatibilidade / escolha manual) */}
      {job.media_type === 'tv' && <SeriesGate job={job} onResolved={reload} />}
      {/* relatório final da série (com recriação das falhas) */}
      {job.media_type === 'tv' && <SeriesReport job={job} />}

      {job.media_type !== 'tv' && job.status === 'awaiting' && job.awaiting?.reason === 'alignment_review' && (
        <AlignmentReview job={job} onResolved={reload} />
      )}

      {job.status === 'awaiting' && job.drift_confirm && (
        <section className="mt-6 rounded-xl border border-amber-900/60 bg-amber-950/20 p-4">
          <h2 className="mb-2 flex items-center gap-1.5 font-semibold text-amber-300">
            <WarningTriangle width={16} height={16} /> Possível versão/corte diferente
          </h2>
          <p className="text-sm text-zinc-300">
            O offset varia entre o início (
            <span className="tabular-nums">{job.drift_confirm.tau1_ms.toFixed(0)} ms</span>) e o meio (
            <span className="tabular-nums">{job.drift_confirm.tau2_ms.toFixed(0)} ms</span>) do filme — o áudio
            pode dessincronizar. Conversão pausada.
          </p>
          {job.drift_confirm.profile ? (
            <div className="mt-3 text-xs text-zinc-400">
              <div className="mb-1">
                Perfil do offset a cada 5 min:{' '}
                <span className="font-semibold text-zinc-200">
                  {{
                    drift: 'drift — muda aos poucos (velocidade/frame rate)',
                    cut: 'corte — salto entre patamares (cena/junção diferente)',
                    flat: 'constante nas janelas medidas',
                    mixed: 'misto',
                    unknown: 'poucas janelas para dizer',
                  }[job.drift_confirm.verdict ?? 'unknown']}
                </span>
              </div>
              <div className="flex flex-wrap gap-1">
                {job.drift_confirm.profile.map((p) => (
                  <span key={p.t} className="rounded bg-zinc-800/80 px-1.5 py-0.5 tabular-nums">
                    {Math.round(p.t / 60)}min {p.offset_ms >= 0 ? '+' : ''}{p.offset_ms.toFixed(0)}ms
                  </span>
                ))}
              </div>
            </div>
          ) : /Medindo/.test(job.detail ?? '') ? (
            <p className="mt-2 text-xs text-zinc-500">Medindo o offset ao longo do filme…</p>
          ) : null}
          <div className="mt-3 flex flex-wrap gap-2">
            <button
              onClick={() => proceedAnyway('advanced')}
              disabled={submitting}
              className="inline-flex items-center gap-1.5 rounded-lg bg-amber-600 px-4 py-2 text-sm font-semibold text-zinc-950 hover:bg-amber-500 disabled:opacity-50"
            >
              <Play width={15} height={15} /> Rodar alinhamento avançado
            </button>
            <button
              onClick={() => proceedAnyway('offset')}
              disabled={submitting}
              className="inline-flex items-center gap-1.5 rounded-lg border border-amber-700/60 px-4 py-2 text-sm font-semibold text-amber-200 hover:bg-amber-900/30 disabled:opacity-50"
            >
              Continuar com o offset do início
            </button>
          </div>
          <p className="mt-2 text-xs text-zinc-500">
            O avançado é o alinhador das séries (por conteúdo, com EDL e revisão): resolve cena/junção
            diferente no mesmo corte.{job.search ? ' Ou escolha outro torrent abaixo, ou cancele o job.' : ''}
          </p>
        </section>
      )}

      {job.status === 'awaiting' && job.search && (
        <section className="mt-6 rounded-xl border border-purple-900/60 bg-purple-950/20 p-4">
          <h2 className="mb-3 font-semibold text-purple-300">
            {needAudio && needVideo ? 'Escolha os torrents' : 'Escolha o torrent'}
          </h2>
          {needAudio && (
            <>
              <h3 className="mb-2 flex items-center gap-1.5 text-sm text-zinc-400">
                <SoundHigh width={14} height={14} /> Áudio ({job.language})
              </h3>
              <CandidatesTable candidates={job.search.audio} selectable selectedId={selAudio} onSelect={setSelAudio} />
            </>
          )}
          {needVideo && (
            <>
              <h3 className="mt-4 mb-2 flex items-center gap-1.5 text-sm text-zinc-400">
                <MediaVideo width={14} height={14} /> Vídeo (original)
              </h3>
              <CandidatesTable candidates={job.search.video} selectable selectedId={selVideo} onSelect={setSelVideo} />
            </>
          )}
          <button
            onClick={submitSelection}
            disabled={submitting || !selectionReady}
            className="mt-3 inline-flex items-center gap-1.5 rounded-lg bg-blue-600 px-4 py-2 text-sm font-semibold hover:bg-blue-500 disabled:opacity-50"
          >
            <Download width={15} height={15} /> Confirmar e baixar
          </button>
        </section>
      )}

      {/* ---- conversão/cópia: barra (se ffmpeg) + tempo decorrido ---- */}
      {(job.merge_started_at || (job.status === 'merging' && job.progress?.merge)) && (
        <section className="mt-6">
          <div className="mb-2 flex items-center gap-2">
            {/* enquanto o fingerprint roda não é conversão: é a leitura dos
                dois arquivos para achar o alinhamento */}
            <h2 className={`text-sm font-semibold ${alignRole(job.progress?.merge) ? 'text-amber-300' : 'text-zinc-400'}`}>
              {alignRole(job.progress?.merge)
                ? `Buscando alinhamento (${alignRole(job.progress?.merge)})`
                : 'Conversão'}
            </h2>
            {job.merge_started_at && (
              <Elapsed
                since={job.merge_started_at}
                running={job.status === 'merging'}
                title="Tempo desde o início da conversão/cópia"
              />
            )}
          </div>
          {job.status === 'merging' && job.progress?.merge && <MergeBar p={job.progress.merge} />}
        </section>
      )}

      {/* série: todos os torrents em andamento + estado por episódio */}
      {job.media_type === 'tv' && <SeriesTorrents job={job} onChanged={reload} />}
      {job.media_type === 'tv' && <SeriesEpisodes job={job} />}

      {(pv || pa) && (
        <section className="mt-6">
          <h2 className="mb-2 text-sm font-semibold text-zinc-400">Downloads</h2>
          {pv && (
            <>
              {pv.name && <div className="truncate text-xs text-zinc-500">{pv.name}</div>}
              <ProgressBar label="Vídeo" p={pv} />
              {switchControls('video')}
            </>
          )}
          {pa && (
            <div className="mt-3">
              {pa.name && <div className="truncate text-xs text-zinc-500">{pa.name}</div>}
              <ProgressBar label="Áudio" p={pa} />
              {switchControls('audio')}
            </div>
          )}
        </section>
      )}

      {/* ---- o que foi escolhido / configurado (resumo em destaque) ---- */}
      <JobSummary job={job} />

      {/* ---- eventos recentes + timeline completa recolhida ---- */}
      {timeline.length > 0 && (
        <section className="mt-6">
          <div className="mb-2 flex items-center gap-3">
            <h2 className="text-sm font-semibold text-zinc-400">
              {fullTimeline ? 'Timeline completa' : 'Eventos recentes'}
            </h2>
            {timeline.length > recent.length && (
              <button
                onClick={() => setFullTimeline((v) => !v)}
                className="rounded-lg border border-zinc-700 px-2 py-0.5 text-xs text-zinc-400 hover:bg-zinc-800 hover:text-zinc-200"
              >
                {fullTimeline ? 'Mostrar só os recentes' : `Ver timeline completa (${timeline.length})`}
              </button>
            )}
          </div>
          <EventList events={fullTimeline ? timeline : recent} />
        </section>
      )}

      {/* ---- candidatos avaliados: informação secundária, em dropdown ---- */}
      {candEvents.length > 0 && (
        <section className="mt-6 space-y-2">
          {candEvents.map((ev, i) => {
            // torrent em uso na role desta avaliação (áudio/vídeo) — destaca o bookmark
            const inUse = ev.data!.role === 'video' ? job.video_torrent : ev.data!.role === 'audio' ? job.audio_torrent : null
            return (
              <Collapsible
                key={i}
                flush
                title={ev.message}
                right={<span className="text-xs text-zinc-500">{ev.data!.candidates!.length} candidatos</span>}
              >
                <CandidatesTable candidates={ev.data!.candidates!} showReason current={inUse} flush />
              </Collapsible>
            )
          })}
        </section>
      )}
    </div>
  )
}

/** Linha do tempo (bolinha colorida + horário + mensagem). */
function EventList({ events }: { events: JobEvent[] }) {
  return (
    <div className="ml-1.5 border-l-2 border-zinc-800 pl-4 text-sm">
      {events.map((ev, i) => (
        <div key={i} className="relative mb-2">
          <span
            className={`absolute top-1.5 -left-[21px] h-2 w-2 rounded-full ${
              ev.kind === 'chosen' ? 'bg-emerald-400' : ev.kind === 'status' ? 'bg-blue-500' : 'bg-zinc-700'
            }`}
          />
          <span className="mr-2 text-zinc-500 tabular-nums">{ev.ts.slice(11, 19)}</span>
          <span className="wrap-break-word whitespace-pre-wrap">{ev.message}</span>
        </div>
      ))}
    </div>
  )
}

/** Resumo do que foi escolhido/configurado: torrents selecionados, destinos,
 *  arquivos de origem e saída. Fica ACIMA da timeline — é o que o usuário quer
 *  ver de relance, sem caçar na lista de eventos. */
function JobSummary({ job }: { job: Job }) {
  type Row = { icon: React.ReactNode; label: string; value: React.ReactNode }
  const rows: Row[] = []
  const ic = (I: typeof MediaVideo) => <I width={15} height={15} className="text-zinc-400" />

  const torrentValue = (t: NonNullable<Job['video_torrent']>) => (
    <span>
      <span className="break-all">{t.title}</span>
      <span className="text-zinc-500"> · {t.seeders} seeds · corte {t.edition ?? 'normal'} · score {t.score}</span>
    </span>
  )
  if (job.video_torrent)
    rows.push({ icon: ic(MediaVideo), label: 'Vídeo', value: torrentValue(job.video_torrent) })
  if (job.audio_torrent)
    rows.push({ icon: ic(SoundHigh), label: 'Áudio', value: torrentValue(job.audio_torrent) })
  if (job.manual_files) {
    rows.push({ icon: ic(MediaVideo), label: 'Arquivo de vídeo', value: <span className="font-mono text-xs break-all">{job.manual_files.video}</span> })
    rows.push({ icon: ic(SoundHigh), label: 'Arquivo de áudio', value: <span className="font-mono text-xs break-all">{job.manual_files.audio}</span> })
  }
  if (job.destination_label) {
    rows.push({
      icon: ic(Folder), label: 'Destino final',
      value: <span>{job.destination_label} <span className="font-mono text-xs text-zinc-500">({job.destination_path})</span></span>,
    })
  }
  if (job.torrent_target_label) {
    rows.push({
      icon: ic(Magnet), label: 'Torrents',
      value: (
        <span>
          {job.torrent_target_label}{' '}
          <span className="font-mono text-xs text-zinc-500">
            ({job.torrent_save_path || 'pasta padrão do qBittorrent'}
            {job.torrent_local_path ? ` → ${job.torrent_local_path}` : ''})
          </span>
        </span>
      ),
    })
  }
  // opções avançadas de conversão (só quando habilitadas)
  const conv = convertSummary(job.convert)
  if (conv.length) {
    rows.push({
      icon: ic(SettingsIcon), label: 'Conversão',
      value: (
        <span className="flex flex-wrap gap-1">
          {conv.map((c, i) => (
            <span key={i} className="rounded bg-purple-950 px-1.5 py-0.5 text-xs font-medium text-purple-300">
              {c}
            </span>
          ))}
        </span>
      ),
    })
  }
  if (job.output)
    rows.push({ icon: ic(Check), label: 'Saída', value: <span className="break-all">{job.output}</span> })

  if (!rows.length) return null
  return (
    <section className="mt-6">
      <h2 className="mb-2 text-sm font-semibold text-zinc-400">Resumo</h2>
      <dl className="divide-y divide-zinc-800/60 rounded-xl border border-zinc-800">
        {rows.map((r, i) => (
          <div key={i} className="grid grid-cols-[8rem_1fr] gap-3 px-3 py-2 text-sm">
            <dt className="flex items-center gap-1.5 text-zinc-400">{r.icon} {r.label}</dt>
            <dd className="min-w-0 text-zinc-200">{r.value}</dd>
          </div>
        ))}
      </dl>
    </section>
  )
}
