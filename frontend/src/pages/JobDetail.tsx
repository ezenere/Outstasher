import { useCallback, useEffect, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { NavArrowLeft, Refresh, Trash } from 'iconoir-react'
import { post, prog, type Job } from '../api'
import { api } from '../api'
import { Badge, CandidatesTable, Empty, MergeBar, ProgressBar } from '../components/ui'
import { jobTitle, kindLabel, removeJob } from './Jobs'

export default function JobDetail() {
  const { id } = useParams<{ id: string }>()
  const [job, setJob] = useState<Job | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [selAudio, setSelAudio] = useState<string | undefined>()
  const [selVideo, setSelVideo] = useState<string | undefined>()
  const [submitting, setSubmitting] = useState(false)
  const navigate = useNavigate()

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
    const t = setInterval(reload, 2000)
    return () => clearInterval(t)
  }, [reload])

  // pre-seleciona os melhores quando o job esta aguardando escolha
  useEffect(() => {
    if (job?.status === 'awaiting' && job.search) {
      setSelAudio((cur) => cur ?? job.search!.audio[0]?.id)
      setSelVideo((cur) => cur ?? job.search!.video[0]?.id)
    }
  }, [job?.status, job?.search])

  if (error) return <Empty>Erro: {error}</Empty>
  if (!job) return <Empty>Carregando...</Empty>

  const pv = prog(job.progress?.video)
  const pa = prog(job.progress?.audio)
  const candEvents = (job.events ?? []).filter((e) => e.kind === 'candidates' && e.data?.candidates)
  const timeline = (job.events ?? []).filter((e) => e.kind !== 'candidates')

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
      if (ea !== ev && !confirm(
        `Cortes diferentes (${ea ?? 'normal'} ≠ ${ev ?? 'normal'}) — os áudios provavelmente NÃO vão alinhar.\nContinuar mesmo assim?`,
      )) return
    }
    setSubmitting(true)
    try {
      await post(`/api/jobs/${job.id}/select`, {
        audio_id: needAudio ? selAudio : null,
        video_id: needVideo ? selVideo : null,
      })
      void reload()
    } catch (e) {
      alert(`Erro: ${(e as Error).message}`)
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div>
      <div className="flex flex-wrap items-center gap-3">
        <Link to="/jobs" className="rounded-lg border border-zinc-700 p-1.5 text-zinc-400 hover:text-zinc-200">
          <NavArrowLeft width={16} height={16} />
        </Link>
        <h1 className="flex-1 text-lg font-semibold">
          {jobTitle(job)} <small className="font-normal text-zinc-400">— {kindLabel(job)}</small>
        </h1>
        <Badge status={job.status} />
        {(job.status === 'error' || job.status === 'cancelled') && (
          <button
            onClick={() => post(`/api/jobs/${job.id}/retry`).then(() => navigate('/jobs')).catch((e) => alert((e as Error).message))}
            title="Tentar de novo"
            className="rounded-lg border border-zinc-700 p-1.5 text-zinc-400 hover:text-zinc-200"
          >
            <Refresh width={15} height={15} />
          </button>
        )}
        <button
          onClick={() => removeJob(job.id, () => navigate('/jobs'))}
          title="Remover job"
          className="rounded-lg border border-zinc-700 p-1.5 text-zinc-400 hover:text-zinc-200"
        >
          <Trash width={15} height={15} />
        </button>
      </div>

      <div className="mt-2 text-sm whitespace-pre-wrap text-zinc-400">{job.detail}</div>

      {job.status === 'awaiting' && job.search && (
        <section className="mt-6 rounded-xl border border-purple-900/60 bg-purple-950/20 p-4">
          <h2 className="mb-3 font-semibold text-purple-300">
            {needAudio && needVideo ? 'Escolha os torrents' : 'Escolha o torrent'}
          </h2>
          {needAudio && (
            <>
              <h3 className="mb-2 text-sm text-zinc-400">🔊 Áudio ({job.language})</h3>
              <CandidatesTable candidates={job.search.audio} selectable selectedId={selAudio} onSelect={setSelAudio} />
            </>
          )}
          {needVideo && (
            <>
              <h3 className="mt-4 mb-2 text-sm text-zinc-400">🎥 Vídeo (original)</h3>
              <CandidatesTable candidates={job.search.video} selectable selectedId={selVideo} onSelect={setSelVideo} />
            </>
          )}
          <button
            onClick={submitSelection}
            disabled={submitting || !selectionReady}
            className="mt-3 rounded-lg bg-blue-600 px-4 py-2 text-sm font-semibold hover:bg-blue-500 disabled:opacity-50"
          >
            ⬇ Confirmar e baixar
          </button>
        </section>
      )}

      {job.status === 'merging' && job.progress?.merge && (
        <section className="mt-6">
          <h2 className="mb-2 text-sm font-semibold text-zinc-400">Conversão</h2>
          <MergeBar p={job.progress.merge} />
        </section>
      )}

      {(pv || pa) && (
        <section className="mt-6">
          <h2 className="mb-2 text-sm font-semibold text-zinc-400">Downloads</h2>
          {pv && (
            <>
              {pv.name && <div className="truncate text-xs text-zinc-500">{pv.name}</div>}
              <ProgressBar label="Vídeo" p={pv} />
            </>
          )}
          {pa && (
            <div className="mt-3">
              {pa.name && <div className="truncate text-xs text-zinc-500">{pa.name}</div>}
              <ProgressBar label="Áudio" p={pa} />
            </div>
          )}
        </section>
      )}

      {candEvents.map((ev, i) => (
        <section key={i} className="mt-6">
          <h2 className="mb-2 text-sm font-semibold text-zinc-400">{ev.message}</h2>
          <CandidatesTable candidates={ev.data!.candidates!} showReason />
        </section>
      ))}

      {timeline.length > 0 && (
        <section className="mt-6">
          <h2 className="mb-2 text-sm font-semibold text-zinc-400">Timeline</h2>
          <div className="ml-1.5 border-l-2 border-zinc-800 pl-4 text-sm">
            {timeline.map((ev, i) => (
              <div key={i} className="relative mb-2">
                <span
                  className={`absolute top-1.5 -left-[21px] h-2 w-2 rounded-full ${
                    ev.kind === 'chosen' ? 'bg-emerald-400' : ev.kind === 'status' ? 'bg-blue-500' : 'bg-zinc-700'
                  }`}
                />
                <span className="mr-2 text-zinc-500 tabular-nums">{ev.ts.slice(11, 19)}</span>
                <span className="whitespace-pre-wrap">{ev.message}</span>
              </div>
            ))}
          </div>
        </section>
      )}

      {job.destination_label && (
        <section className="mt-6">
          <h2 className="mb-1 text-sm font-semibold text-zinc-400">Destino do arquivo final</h2>
          <div className="text-sm">
            {job.destination_label}{' '}
            <span className="font-mono text-xs text-zinc-500">({job.destination_path})</span>
          </div>
        </section>
      )}

      {job.torrent_target_label && (
        <section className="mt-6">
          <h2 className="mb-1 text-sm font-semibold text-zinc-400">Destino dos torrents</h2>
          <div className="text-sm">
            {job.torrent_target_label}{' '}
            <span className="font-mono text-xs text-zinc-500">
              ({job.torrent_save_path || 'pasta padrão do qBittorrent'}
              {job.torrent_local_path ? ` → ${job.torrent_local_path}` : ''})
            </span>
          </div>
        </section>
      )}

      {job.output && (
        <section className="mt-6">
          <h2 className="mb-1 text-sm font-semibold text-zinc-400">Saída</h2>
          <div className="text-sm break-all">{job.output}</div>
        </section>
      )}
    </div>
  )
}
