import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { Search, Xmark } from 'iconoir-react'
import {
  api, MOVIE_STATE_LABEL, movieStates, post,
  type Destination, type Job, type Language, type Movie, type TorrentTarget,
} from '../api'
import { DiskFree, Empty, MovieStateBadge, MovieStateIcon } from '../components/ui'

export default function Movies() {
  const [movies, setMovies] = useState<Movie[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [query, setQuery] = useState('')
  const [languages, setLanguages] = useState<Language[]>([])
  const [language, setLanguage] = useState('pt')
  const [manual, setManual] = useState(false)
  const [destinations, setDestinations] = useState<Destination[]>([])
  const [destId, setDestId] = useState<number | null>(null)
  const [targets, setTargets] = useState<TorrentTarget[]>([])
  const [targetId, setTargetId] = useState<number | null>(null)
  const [selected, setSelected] = useState<Movie | null>(null)
  const [starting, setStarting] = useState(false)
  const [jobs, setJobs] = useState<Job[]>([])
  const [justStarted, setJustStarted] = useState<number | null>(null)

  // estado de cada filme (tmdb_id -> convertendo/baixando/... ) a partir dos jobs
  const states = useMemo(() => movieStates(jobs), [jobs])

  useEffect(() => {
    async function loadJobs() {
      try {
        setJobs(await api<Job[]>('/api/jobs'))
      } catch {
        /* servidor reiniciando; proxima tentativa no tick */
      }
    }
    void loadJobs()
    const t = setInterval(loadJobs, 4000)
    return () => clearInterval(t)
  }, [])

  useEffect(() => {
    api<Language[]>('/api/languages').then(setLanguages).catch(() => {})
    api<Destination[]>('/api/destinations')
      .then((ds) => {
        setDestinations(ds)
        setDestId(ds.find((d) => d.is_default)?.id ?? ds[0]?.id ?? null)
      })
      .catch(() => {})
    api<TorrentTarget[]>('/api/torrent-targets')
      .then((ts) => {
        setTargets(ts)
        setTargetId(ts.find((t) => t.is_default)?.id ?? ts[0]?.id ?? null)
      })
      .catch(() => {})
    void search('')
  }, [])

  async function search(q: string) {
    setMovies(null)
    setError(null)
    try {
      setMovies(await api<Movie[]>(`/api/movies?q=${encodeURIComponent(q)}`))
    } catch (e) {
      setError((e as Error).message)
    }
  }

  async function start(kind: 'both' | 'original' | 'dubbed') {
    if (!selected) return
    // avisa se o filme já está sendo baixado/convertido/finalizado
    const existing = states.get(selected.id)
    if (existing && !confirm(
      `Este filme já tem um download ${MOVIE_STATE_LABEL[existing].toLowerCase()}.\n`
      + 'Quer baixar de novo mesmo assim?',
    )) return
    const tmdbId = selected.id
    setStarting(true)
    try {
      const job = await post<Job>('/api/jobs', {
        tmdb_id: tmdbId,
        language,
        mode: manual ? 'manual' : 'auto',
        kind,
        destination_id: destId,
        torrent_target_id: targetId,
      })
      // some com a seleção e sinaliza que começou (sem trocar de tela)
      setSelected(null)
      setJobs((js) => [job, ...js])
      setJustStarted(tmdbId)
      setTimeout(() => setJustStarted((cur) => (cur === tmdbId ? null : cur)), 4000)
    } catch (e) {
      alert(`Erro ao criar job: ${(e as Error).message}`)
    } finally {
      setStarting(false)
    }
  }

  return (
    <div>
      <div className="flex gap-2">
        <div className="relative flex-1">
          <Search width={16} height={16} className="absolute top-1/2 left-3 -translate-y-1/2 text-zinc-500" />
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && search(query)}
            placeholder="Buscar filme no TMDB... (vazio = populares)"
            className="w-full rounded-lg border border-zinc-700 bg-zinc-900 py-2.5 pr-3 pl-9 text-sm outline-none focus:border-blue-500"
          />
        </div>
        <button
          onClick={() => search(query)}
          className="rounded-lg bg-blue-600 px-4 text-sm font-semibold hover:bg-blue-500"
        >
          Buscar
        </button>
      </div>

      <div className="mt-6 grid grid-cols-2 gap-4 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5">
        {movies === null && !error && <Empty>Carregando...</Empty>}
        {error && <Empty>Erro: {error}</Empty>}
        {movies?.length === 0 && <Empty>Nada encontrado.</Empty>}
        {movies?.map((m) => {
          const state = states.get(m.id)
          const started = justStarted === m.id
          return (
            <button
              key={m.id}
              onClick={() => setSelected(m)}
              className={`flex flex-col relative overflow-hidden rounded-xl border-2 bg-zinc-900 text-left transition-transform hover:-translate-y-1 ${
                selected?.id === m.id ? 'border-blue-500' : state ? 'border-zinc-700' : 'border-transparent'
              }`}
            >
              {(state || started) && (
                <div className="absolute top-1.5 left-1.5 z-10 flex items-center gap-1 rounded-md bg-black/70 px-1.5 py-0.5 backdrop-blur">
                  {started && !state ? (
                    <MovieStateIcon state="downloading" />
                  ) : state ? (
                    <MovieStateIcon state={state} />
                  ) : null}
                </div>
              )}
              {m.poster ? (
                <img src={m.poster} loading="lazy" className="aspect-[2/3] w-full bg-zinc-800 object-cover" />
              ) : (
                <div className="flex aspect-[2/3] w-full items-center justify-center bg-zinc-800 p-2 text-center text-sm text-zinc-500">
                  {m.title ?? '?'}
                </div>
              )}
              <div className="px-2.5 py-2 min-h-16">
                <div className="text-sm leading-tight font-semibold">{m.title ?? m.original_title}</div>
                <div className="mt-0.5 text-xs text-zinc-400">
                  {m.year} {m.rating ? `· ⭐ ${m.rating.toFixed(1)}` : ''}
                </div>
                {state && (
                  <div className="mt-1.5">
                    <MovieStateBadge state={state} />
                  </div>
                )}
                {started && !state && (
                  <div className="mt-1.5 text-xs font-medium text-yellow-300">Iniciando download…</div>
                )}
              </div>
            </button>
          )
        })}
      </div>

      {selected && (
        <div className="fixed inset-x-0 bottom-0 z-20 border-t border-zinc-800 bg-zinc-900/95 backdrop-blur">
          <div className="mx-auto flex max-w-5xl flex-col gap-3 px-4 py-3">
            {/* linha 1: título */}
            <div className="font-semibold">{selected.title ?? selected.original_title}</div>

            {/* linha 2: Áudio / Destino / Torrents, cada um em 1/3 */}
            <div className="flex flex-col gap-3 sm:flex-row">
              <label className="flex flex-1 flex-col gap-1 text-sm">
                <span className="text-zinc-400">Áudio</span>
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
                <span className="flex items-center gap-2 text-zinc-400">
                  Destino
                  <DiskFree disk={destinations.find((d) => d.id === destId)?.disk} />
                </span>
                {destinations.length ? (
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
                ) : (
                  <Link to="/settings" className="py-1.5 text-blue-400 underline">
                    cadastrar destino
                  </Link>
                )}
              </label>

              <label className="flex flex-1 flex-col gap-1 text-sm">
                <span className="flex items-center gap-2 text-zinc-400">
                  Torrents
                  <DiskFree disk={targets.find((t) => t.id === targetId)?.disk} />
                </span>
                {targets.length ? (
                  <select
                    value={targetId ?? ''}
                    onChange={(e) => setTargetId(Number(e.target.value))}
                    className="rounded-lg border border-zinc-700 bg-zinc-800 px-2 py-1.5 text-sm"
                    title={targets.find((t) => t.id === targetId)?.save_path || 'pasta padrão do qBittorrent'}
                  >
                    {targets.map((t) => (
                      <option key={t.id} value={t.id}>
                        {t.label}{t.is_default ? ' ★' : ''}
                      </option>
                    ))}
                  </select>
                ) : (
                  <span className="py-1.5 text-zinc-500" title="Sem destino de torrents: usa a pasta padrão do qBittorrent">
                    padrão do qBittorrent
                  </span>
                )}
              </label>
            </div>

            {/* linha 3: manual à esquerda, ações à direita */}
            <div className="flex items-center gap-3">
              <label className="flex items-center gap-1.5 text-sm text-zinc-300">
                <input type="checkbox" checked={manual} onChange={(e) => setManual(e.target.checked)} />
                Escolher torrents manualmente
              </label>
              <div className="ml-auto flex flex-wrap items-center justify-end gap-2">
                <button
                  onClick={() => start('original')}
                  disabled={starting || !destinations.length}
                  title="Baixa só o vídeo no idioma original, sem merge"
                  className="rounded-lg border border-zinc-600 px-3 py-2 text-sm font-medium text-zinc-200 hover:bg-zinc-800 disabled:opacity-50"
                >
                  🎥 Só original
                </button>
                <button
                  onClick={() => start('dubbed')}
                  disabled={starting || !destinations.length}
                  title="Baixa só a versão dublada, sem merge"
                  className="rounded-lg border border-zinc-600 px-3 py-2 text-sm font-medium text-zinc-200 hover:bg-zinc-800 disabled:opacity-50"
                >
                  🔊 Só dublado
                </button>
                <button
                  onClick={() => start('both')}
                  disabled={starting || !destinations.length}
                  className="rounded-lg bg-blue-600 px-4 py-2 text-sm font-semibold hover:bg-blue-500 disabled:opacity-50"
                >
                  ⬇ Baixar e fazer merge
                </button>
                <button
                  onClick={() => setSelected(null)}
                  className="rounded-lg border border-zinc-700 p-2 text-zinc-400 hover:text-zinc-200"
                  title="Cancelar"
                >
                  <Xmark width={16} height={16} />
                </button>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
