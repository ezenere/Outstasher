import { useEffect, useState } from 'react'
import { Label, Movie as MovieIcon, Search, Xmark } from 'iconoir-react'
import { api, type Movie, type MoviePage } from '../api'
import { useScrollLock } from './ui'

interface Props {
  /** Título da pasta local (o nome proposto é derivado dele, não do TMDB). */
  title: string
  year?: string | null
  /** Match automático do backend; vem pré-selecionado. Pode ser trocado. */
  suggested: Movie | null
  onConfirm: (tmdbId: number) => Promise<void> | void
  onClose: () => void
}

/** Escolha do filme antes de marcar a pasta com [tmdbid-N].
 *
 *  O match automático do TMDB erra em remake/título localizado/coletânea, e a
 *  marcação renomeia a pasta — então dá para confirmar (ou trocar) o filme
 *  antes. Mesma busca do modal de adicionar filme manualmente.
 */
export default function TmdbPickerModal({ title, year, suggested, onConfirm, onClose }: Props) {
  const [movie, setMovie] = useState<Movie | null>(suggested)
  const [query, setQuery] = useState('')
  const [results, setResults] = useState<Movie[] | null>(null)
  const [searching, setSearching] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  useScrollLock()

  // busca já pré-preenchida com o título da pasta: o caso comum é procurar o
  // mesmo filme, só que a entrada certa
  useEffect(() => {
    setQuery(title)
  }, [title])

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

  async function confirm() {
    if (!movie || submitting) return
    setSubmitting(true)
    setError(null)
    try {
      await onConfirm(movie.id)
    } catch (e) {
      setError((e as Error).message)
      setSubmitting(false)  // erro: deixa tentar de novo (sucesso desmonta o modal)
    }
  }

  // o nome proposto usa o título/ano da PASTA (é o que a renomeação faz);
  // só o [tmdbid-N] muda conforme o filme escolhido
  const base = year ? `${title} (${year})` : title
  const proposed = movie ? `${base} [tmdbid-${movie.id}]` : null

  return (
    <div className="fixed inset-0 z-30 flex justify-center overflow-y-auto bg-black/60 p-4" onClick={onClose}>
      <div
        className="my-auto h-fit w-full max-w-lg rounded-2xl border border-zinc-700 bg-zinc-900 p-5"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center gap-2">
          <h2 className="flex-1 text-lg font-semibold">Marcar ID do TMDB</h2>
          <button onClick={onClose} className="rounded-lg border border-zinc-700 p-1.5 text-zinc-400 hover:text-zinc-200" title="Fechar">
            <Xmark width={16} height={16} />
          </button>
        </div>
        <p className="mt-1 text-sm text-zinc-400">
          Confirme o filme antes de renomear a pasta. O Jellyfin usa esse ID para
          identificar o filme sem depender do título.
        </p>

        <div className="mt-4">
          {movie ? (
            <div className="flex items-center gap-3 rounded-lg border border-blue-800 bg-blue-950/30 px-3 py-2">
              {movie.poster && <img src={movie.poster} className="h-14 w-9 rounded bg-zinc-800 object-cover" alt="" />}
              <div className="min-w-0 flex-1">
                <div className="truncate font-semibold">{movie.title ?? movie.original_title}</div>
                <div className="text-xs text-zinc-400">{movie.year} · TMDB #{movie.id}</div>
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
                    autoFocus
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

        {proposed && (
          <p className="mt-4 text-sm text-zinc-400">
            A pasta será renomeada para{' '}
            <span className="font-mono text-zinc-200">{proposed}</span>
          </p>
        )}
        {error && <p className="mt-3 text-sm text-red-300">{error}</p>}

        <div className="mt-5 flex justify-end gap-2">
          <button onClick={onClose} className="rounded-lg border border-zinc-700 px-4 py-2 text-sm font-medium text-zinc-300 hover:bg-zinc-800">
            Cancelar
          </button>
          <button
            onClick={confirm}
            disabled={!movie || submitting}
            className="inline-flex items-center gap-1.5 rounded-lg bg-blue-600 px-4 py-2 text-sm font-semibold hover:bg-blue-500 disabled:opacity-50"
          >
            <Label width={15} height={15} /> {submitting ? 'Renomeando...' : 'Renomear'}
          </button>
        </div>
      </div>
    </div>
  )
}
