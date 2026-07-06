import { useEffect, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { Refresh, Search, Trash } from 'iconoir-react'
import { api, fmtSize, post, prog, type Job } from '../api'
import { Badge, Empty, ProgressBar } from '../components/ui'

export function jobTitle(j: Job): string {
  return j.movie ? `${j.movie.original_title} (${j.movie.year})` : `TMDB #${j.tmdb_id}`
}

export async function removeJob(id: string, reload: () => void) {
  if (!confirm('Remover este job?')) return
  const delT = confirm(
    'Apagar também os torrents e arquivos baixados no qBittorrent?\n\nOK = apagar tudo · Cancelar = manter os downloads',
  )
  try {
    await api(`/api/jobs/${id}?delete_torrents=${delT}`, { method: 'DELETE' })
    reload()
  } catch (e) {
    alert(`Erro: ${(e as Error).message}`)
  }
}

export default function Jobs() {
  const [jobs, setJobs] = useState<Job[] | null>(null)
  const navigate = useNavigate()

  async function reload() {
    try {
      setJobs(await api<Job[]>('/api/jobs'))
    } catch {
      /* servidor reiniciando; proxima tentativa no tick */
    }
  }

  useEffect(() => {
    void reload()
    const t = setInterval(reload, 4000)
    return () => clearInterval(t)
  }, [])

  async function retry(id: string) {
    try {
      await post(`/api/jobs/${id}/retry`)
      void reload()
    } catch (e) {
      alert(`Erro: ${(e as Error).message}`)
    }
  }

  if (jobs === null) return <Empty>Carregando...</Empty>
  if (!jobs.length) return <Empty>Nenhum job ainda. Escolha um filme na aba Filmes.</Empty>

  return (
    <div className="flex flex-col gap-3">
      {jobs.map((j) => {
        const pv = prog(j.progress?.video)
        const pa = prog(j.progress?.audio)
        return (
          <div key={j.id} className="flex gap-3 rounded-xl bg-zinc-900 px-4 py-3.5">
            {j.movie?.poster ? (
              <img
                src={j.movie.poster}
                loading="lazy"
                className="hidden h-24 w-16 shrink-0 rounded-md bg-zinc-800 object-cover sm:block"
                alt=""
              />
            ) : (
              <div className="hidden h-24 w-16 shrink-0 items-center justify-center rounded-md bg-zinc-800 text-zinc-600 sm:flex">
                🎬
              </div>
            )}
            <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-2">
              <span className="flex-1 font-semibold">
                {jobTitle(j)}{' '}
                <small className="font-normal text-zinc-400">
                  [áudio: {j.language}{j.mode === 'manual' ? ' · manual' : ''}
                  {j.destination_label ? ` · ${j.destination_label}` : ''}]
                </small>
              </span>
              <Badge status={j.status} />
              {j.status === 'awaiting' && (
                <button
                  onClick={() => navigate(`/jobs/${j.id}`)}
                  className="rounded-lg bg-blue-600 px-3 py-1.5 text-sm font-semibold hover:bg-blue-500"
                >
                  Escolher
                </button>
              )}
              {(j.status === 'error' || j.status === 'cancelled') && (
                <IconBtn title="Tentar de novo" onClick={() => retry(j.id)}>
                  <Refresh width={15} height={15} />
                </IconBtn>
              )}
              <Link to={`/jobs/${j.id}`} title="Ver detalhes"
                className="rounded-lg border border-zinc-700 p-1.5 text-zinc-400 hover:text-zinc-200">
                <Search width={15} height={15} />
              </Link>
              <IconBtn title="Remover job" onClick={() => removeJob(j.id, reload)}>
                <Trash width={15} height={15} />
              </IconBtn>
            </div>
            <div className="mt-1.5 text-sm whitespace-pre-wrap text-zinc-400">{j.detail}</div>
            {(j.video_torrent || j.audio_torrent) && (
              <div className="mt-2 text-xs text-zinc-500">
                {j.video_torrent && (
                  <div className="truncate">
                    🎥 {j.video_torrent.title} ({j.video_torrent.seeders} seeds, {fmtSize(j.video_torrent.size)})
                  </div>
                )}
                {j.audio_torrent && (
                  <div className="truncate">
                    🔊 {j.audio_torrent.title} ({j.audio_torrent.seeders} seeds, {fmtSize(j.audio_torrent.size)})
                  </div>
                )}
              </div>
            )}
            {j.status === 'downloading' && (
              <>
                <ProgressBar label="Vídeo" p={pv} />
                <ProgressBar label="Áudio" p={pa} />
              </>
            )}
            </div>
          </div>
        )
      })}
    </div>
  )
}

function IconBtn({ title, onClick, children }: {
  title: string
  onClick: () => void
  children: React.ReactNode
}) {
  return (
    <button
      onClick={onClick}
      title={title}
      className="rounded-lg border border-zinc-700 p-1.5 text-zinc-400 hover:text-zinc-200"
    >
      {children}
    </button>
  )
}
