import { useEffect, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { Search, Xmark } from 'iconoir-react'
import {
  api, post, type Destination, type Job, type Language, type Movie, type TorrentTarget,
} from '../api'
import { DiskFree, Empty } from '../components/ui'

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
  const navigate = useNavigate()

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

  async function start() {
    if (!selected) return
    setStarting(true)
    try {
      await post<Job>('/api/jobs', {
        tmdb_id: selected.id,
        language,
        mode: manual ? 'manual' : 'auto',
        destination_id: destId,
        torrent_target_id: targetId,
      })
      setSelected(null)
      navigate('/jobs')
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
        {movies?.map((m) => (
          <button
            key={m.id}
            onClick={() => setSelected(m)}
            className={`overflow-hidden rounded-xl border-2 bg-zinc-900 text-left transition-transform hover:-translate-y-1 ${
              selected?.id === m.id ? 'border-blue-500' : 'border-transparent'
            }`}
          >
            {m.poster ? (
              <img src={m.poster} loading="lazy" className="aspect-[2/3] w-full bg-zinc-800 object-cover" />
            ) : (
              <div className="flex aspect-[2/3] w-full items-center justify-center bg-zinc-800 p-2 text-center text-sm text-zinc-500">
                {m.title ?? '?'}
              </div>
            )}
            <div className="px-2.5 py-2">
              <div className="text-sm leading-tight font-semibold">{m.title ?? m.original_title}</div>
              <div className="mt-0.5 text-xs text-zinc-400">
                {m.year} {m.rating ? `· ⭐ ${m.rating.toFixed(1)}` : ''}
              </div>
            </div>
          </button>
        ))}
      </div>

      {selected && (
        <div className="fixed inset-x-0 bottom-0 z-20 border-t border-zinc-800 bg-zinc-900/95 backdrop-blur">
          <div className="mx-auto flex max-w-5xl flex-wrap items-center gap-3 px-4 py-3">
            <span className="min-w-48 flex-1 font-semibold">{selected.title ?? selected.original_title}</span>
            <label className="flex items-center gap-2 text-sm">
              Áudio:
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
            <label className="flex items-center gap-2 text-sm">
              Destino:
              {destinations.length ? (
                <select
                  value={destId ?? ''}
                  onChange={(e) => setDestId(Number(e.target.value))}
                  className="max-w-48 rounded-lg border border-zinc-700 bg-zinc-800 px-2 py-1.5 text-sm"
                  title={destinations.find((d) => d.id === destId)?.path}
                >
                  {destinations.map((d) => (
                    <option key={d.id} value={d.id}>
                      {d.label}{d.is_default ? ' ★' : ''}
                    </option>
                  ))}
                </select>
              ) : (
                <Link to="/settings" className="text-blue-400 underline">
                  cadastrar destino
                </Link>
              )}
              <DiskFree disk={destinations.find((d) => d.id === destId)?.disk} />
            </label>
            <label className="flex items-center gap-2 text-sm">
              Torrents:
              {targets.length ? (
                <select
                  value={targetId ?? ''}
                  onChange={(e) => setTargetId(Number(e.target.value))}
                  className="max-w-48 rounded-lg border border-zinc-700 bg-zinc-800 px-2 py-1.5 text-sm"
                  title={targets.find((t) => t.id === targetId)?.save_path || 'pasta padrão do qBittorrent'}
                >
                  {targets.map((t) => (
                    <option key={t.id} value={t.id}>
                      {t.label}{t.is_default ? ' ★' : ''}
                    </option>
                  ))}
                </select>
              ) : (
                <span className="text-zinc-500" title="Sem destino de torrents: usa a pasta padrão do qBittorrent">
                  padrão do qBittorrent
                </span>
              )}
              <DiskFree disk={targets.find((t) => t.id === targetId)?.disk} />
            </label>
            <label className="flex items-center gap-1.5 text-sm text-zinc-300">
              <input type="checkbox" checked={manual} onChange={(e) => setManual(e.target.checked)} />
              Escolher torrents manualmente
            </label>
            <button
              onClick={start}
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
      )}
    </div>
  )
}
