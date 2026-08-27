import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Folder, MediaVideo, Movie as MovieIcon, Play, Search, SoundHigh, Xmark } from 'iconoir-react'
import { api, post, type ConvertOptions, type Destination, type Job, type Language, type Movie, type MoviePage } from '../api'
import AdvancedOptions from './AdvancedOptions'
import FolderPicker from './FolderPicker'
import { useScrollLock } from './ui'

interface Props {
  destinations: Destination[]
  defaultDestId: number | null
  onClose: () => void
}

/** Pasta de um caminho, para o navegador reabrir onde o usuário parou. */
const pastaDe = (p: string) => {
  const i = p.trim().lastIndexOf('/')
  return i > 0 ? p.trim().slice(0, i) : null
}

/** Popup de conversão manual: escolhe o filme no TMDB, aponta os dois arquivos
 *  já no disco (pelo navegador de pastas do servidor ou digitando) e o destino
 *  — o merge segue o pipeline normal (alinhamento, fila de conversão,
 *  progresso no detalhe do job). */
export default function AddMovieModal({ destinations, defaultDestId, onClose }: Props) {
  const [languages, setLanguages] = useState<Language[]>([])
  const [language, setLanguage] = useState('pt')
  const [query, setQuery] = useState('')
  const [results, setResults] = useState<Movie[] | null>(null)
  const [searching, setSearching] = useState(false)
  const [movie, setMovie] = useState<Movie | null>(null)
  const [videoPath, setVideoPath] = useState('')
  const [audioPath, setAudioPath] = useState('')
  const [destId, setDestId] = useState<number | null>(defaultDestId)
  const [advanced, setAdvanced] = useState<ConvertOptions | null>(null)
  const [submitting, setSubmitting] = useState(false)
  // qual campo o navegador de pastas está preenchendo
  const [picking, setPicking] = useState<null | 'video' | 'audio'>(null)
  const [error, setError] = useState<string | null>(null)
  const navigate = useNavigate()
  useScrollLock()

  useEffect(() => {
    api<Language[]>('/api/languages').then(setLanguages).catch(() => {})
  }, [])

  async function search() {
    if (!query.trim() || searching) return
    setSearching(true)
    setError(null)
    try {
      const p = await api<MoviePage>(`/api/movies?q=${encodeURIComponent(query.trim())}&page=1`)
      setResults(p.results)
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setSearching(false)
    }
  }

  const ready = movie && videoPath.trim() && audioPath.trim() && destId != null

  async function submit() {
    if (!ready || submitting) return
    setSubmitting(true)
    setError(null)
    try {
      const job = await post<Job>('/api/jobs/manual', {
        tmdb_id: movie!.id,
        language,
        video_path: videoPath.trim(),
        audio_path: audioPath.trim(),
        destination_id: destId,
        convert: advanced,
      })
      navigate(`/jobs/${job.id}`)
    } catch (e) {
      setError((e as Error).message)
      setSubmitting(false)
    }
  }

  return (
    <div className="fixed inset-0 z-30 flex justify-center overflow-y-auto bg-black/60 p-4" onClick={onClose}>
      {/* my-auto (e não items-center) centraliza quando cabe, mas deixa o modal
          crescer para baixo quando o conteúdo passa da tela — com items-center o
          topo saía da viewport e ficava inacessível ao rolar */}
      <div
        className="my-auto h-fit w-full max-w-2xl rounded-2xl border border-zinc-700 bg-zinc-900 p-5"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center gap-2">
          <h2 className="flex-1 text-lg font-semibold">Adicionar filme (conversão manual)</h2>
          <button onClick={onClose} className="rounded-lg border border-zinc-700 p-1.5 text-zinc-400 hover:text-zinc-200" title="Fechar">
            <Xmark width={16} height={16} />
          </button>
        </div>
        <p className="mt-1 text-sm text-zinc-400">
          Faz o merge de dois arquivos que já estão no disco do servidor — mesmo processo dos
          downloads (alinhamento dos áudios e conversão), mas com caminhos digitados à mão.
        </p>

        {/* 1. filme */}
        <div className="mt-4">
          <div className="mb-1 text-sm text-zinc-400">Filme</div>
          {movie ? (
            <div className="flex items-center gap-3 rounded-lg border border-blue-800 bg-blue-950/30 px-3 py-2">
              {movie.poster && <img src={movie.poster} className="h-14 w-9 rounded bg-zinc-800 object-cover" alt="" />}
              <div className="min-w-0 flex-1">
                <div className="truncate font-semibold">{movie.title ?? movie.original_title}</div>
                <div className="text-xs text-zinc-400">{movie.year}</div>
              </div>
              <button onClick={() => setMovie(null)} className="text-sm text-zinc-400 hover:text-zinc-200">
                trocar
              </button>
            </div>
          ) : (
            <>
              <div className="flex gap-2">
                <div className="relative flex-1">
                  <Search width={15} height={15} className="absolute top-1/2 left-3 -translate-y-1/2 text-zinc-500" />
                  <input
                    value={query}
                    onChange={(e) => setQuery(e.target.value)}
                    onKeyDown={(e) => e.key === 'Enter' && search()}
                    placeholder="Buscar filme no TMDB..."
                    className="w-full rounded-lg border border-zinc-700 bg-zinc-800 py-2 pr-3 pl-9 text-sm outline-none focus:border-blue-500"
                  />
                </div>
                <button
                  onClick={search}
                  disabled={searching || !query.trim()}
                  className="rounded-lg bg-blue-600 px-4 text-sm font-semibold hover:bg-blue-500 disabled:opacity-50"
                >
                  {searching ? '...' : 'Buscar'}
                </button>
              </div>
              {results && (
                <div className="mt-2 max-h-56 overflow-y-auto rounded-lg border border-zinc-800">
                  {results.length === 0 && <div className="p-3 text-sm text-zinc-500">Nada encontrado.</div>}
                  {results.map((m) => (
                    <button
                      key={m.id}
                      onClick={() => setMovie(m)}
                      className="flex w-full items-center gap-3 border-b border-zinc-800 px-3 py-2 text-left last:border-b-0 hover:bg-zinc-800/70"
                    >
                      {m.poster ? (
                        <img src={m.poster} loading="lazy" className="h-12 w-8 shrink-0 rounded bg-zinc-800 object-cover" alt="" />
                      ) : (
                        <div className="flex h-12 w-8 shrink-0 items-center justify-center rounded bg-zinc-800 text-zinc-500"><MovieIcon width={16} height={16} /></div>
                      )}
                      <div className="min-w-0 flex-1">
                        <div className="truncate text-sm font-semibold">{m.title ?? m.original_title}</div>
                        <div className="text-xs text-zinc-400">
                          {m.year} {m.rating ? `· ⭐ ${m.rating.toFixed(1)}` : ''}
                        </div>
                      </div>
                    </button>
                  ))}
                </div>
              )}
            </>
          )}
        </div>

        {/* 2. arquivos — pelo navegador do servidor (o mesmo do modal de série)
            ou digitados à mão, para quem já sabe o caminho */}
        <ArquivoField
          id="mm-video" icon={MediaVideo}
          label="Arquivo de vídeo (caminho no servidor)"
          value={videoPath} onChange={setVideoPath}
          placeholder="/mnt/.../Filme.2014.1080p.mkv"
          onBrowse={() => setPicking('video')}
          className="mt-4"
        />
        <ArquivoField
          id="mm-audio" icon={SoundHigh}
          label="Arquivo com o áudio dublado (caminho no servidor)"
          value={audioPath} onChange={setAudioPath}
          placeholder="/mnt/.../Filme.Dublado.mkv"
          onBrowse={() => setPicking('audio')}
          className="mt-3"
        />

        {/* 3. idioma + destino */}
        <div className="mt-3 flex flex-col gap-3 sm:flex-row">
          <label className="flex flex-1 flex-col gap-1 text-sm">
            <span className="text-zinc-400">Idioma do áudio dublado</span>
            <select
              value={language}
              onChange={(e) => setLanguage(e.target.value)}
              className="rounded-lg border border-zinc-700 bg-zinc-800 px-2 py-1.5 text-sm"
            >
              {languages.map((l) => (
                <option key={l.code} value={l.code}>{l.label}</option>
              ))}
            </select>
          </label>
          <label className="flex flex-1 flex-col gap-1 text-sm">
            <span className="text-zinc-400">Pasta de saída (destino)</span>
            <select
              value={destId ?? ''}
              onChange={(e) => setDestId(Number(e.target.value))}
              className="rounded-lg border border-zinc-700 bg-zinc-800 px-2 py-1.5 text-sm"
              title={destinations.find((d) => d.id === destId)?.path}
            >
              {destinations.map((d) => (
                <option key={d.id} value={d.id}>
                  {d.label}{d.is_default ? ' ★' : ''}
                </option>
              ))}
            </select>
          </label>
        </div>

        {/* opções avançadas de conversão (codec/resolução/bitrate/áudios) */}
        <AdvancedOptions value={advanced} onChange={setAdvanced} />

        {error && (
          <div className="mt-3 rounded-lg border border-red-900/60 bg-red-950/30 px-3 py-2 text-sm text-red-300">
            {error}
          </div>
        )}

        <div className="mt-4 flex justify-end gap-2">
          <button onClick={onClose} className="rounded-lg border border-zinc-700 px-4 py-2 text-sm text-zinc-300 hover:bg-zinc-800">
            Cancelar
          </button>
          <button
            onClick={submit}
            disabled={!ready || submitting}
            className="inline-flex items-center gap-1.5 rounded-lg bg-blue-600 px-4 py-2 text-sm font-semibold hover:bg-blue-500 disabled:opacity-50"
          >
            {submitting ? 'Validando arquivos...' : <><Play width={15} height={15} /> Converter</>}
          </button>
        </div>
      </div>

      {picking && (
        <FolderPicker
          mode="file"
          title={picking === 'video' ? 'Arquivo de vídeo (original)'
            : 'Arquivo com o áudio dublado'}
          start={pastaDe(picking === 'video' ? videoPath : audioPath)}
          onPick={(p) => {
            picking === 'video' ? setVideoPath(p) : setAudioPath(p)
            setPicking(null)
          }}
          onClose={() => setPicking(null)}
        />
      )}
    </div>
  )
}

function ArquivoField({ id, icon: Icon, label, value, onChange, placeholder,
                       onBrowse, className }: {
  id: string
  icon: typeof MediaVideo
  label: string
  value: string
  onChange: (v: string) => void
  placeholder: string
  onBrowse: () => void
  className?: string
}) {
  return (
    <div className={`text-sm ${className ?? ''}`}>
      <label htmlFor={id} className="flex items-center gap-1.5 text-zinc-400">
        <Icon width={14} height={14} /> {label}
      </label>
      <div className="mt-1 flex gap-2">
        <input
          id={id}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          placeholder={placeholder}
          className="min-w-0 flex-1 rounded-lg border border-zinc-700 bg-zinc-800 px-3 py-2 font-mono text-xs outline-none focus:border-blue-500"
        />
        <button
          type="button"
          onClick={onBrowse}
          title="Procurar no servidor"
          className="inline-flex shrink-0 items-center gap-1.5 rounded-lg border border-zinc-700 px-3 text-xs text-zinc-300 hover:bg-zinc-800 hover:text-zinc-100"
        >
          <Folder width={14} height={14} /> Procurar
        </button>
      </div>
    </div>
  )
}
