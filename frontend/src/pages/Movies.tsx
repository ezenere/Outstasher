import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { Check, Download, MediaVideo, Search, SoundHigh, Xmark } from 'iconoir-react'
import {
  api, MOVIE_STATE_LABEL, post,
  type ConvertOptions, type Destination, type Job, type Language, type Movie,
  type MovieState, type MoviePage, type TorrentTarget,
} from '../api'
import { useJobsSummary } from '../jobsSummary'
import AdvancedOptions from '../components/AdvancedOptions'
import { DiskFree, Empty, MovieStateBadge, MovieStateIcon } from '../components/ui'

// estados "em progresso": se um filme estava num destes e sumiu do summary,
// entendemos que terminou -> marca como 'done' (Baixado) localmente.
const IN_PROGRESS = new Set<MovieState>(['converting', 'downloading', 'searching', 'awaiting'])

export default function Movies() {
  const [movies, setMovies] = useState<Movie[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [query, setQuery] = useState('')
  const [languages, setLanguages] = useState<Language[]>([])
  const [language, setLanguage] = useState('pt')
  const [manual, setManual] = useState(false)
  const [downloadOnly, setDownloadOnly] = useState(false)
  const [advanced, setAdvanced] = useState<ConvertOptions | null>(null)
  const [destinations, setDestinations] = useState<Destination[]>([])
  const [destId, setDestId] = useState<number | null>(null)
  const [targets, setTargets] = useState<TorrentTarget[]>([])
  const [targetId, setTargetId] = useState<number | null>(null)
  const [selected, setSelected] = useState<Movie | null>(null)
  const [starting, setStarting] = useState(false)
  const [justStarted, setJustStarted] = useState<number | null>(null)
  // paginação: guarda o termo buscado, a página atual e o total de páginas
  const [searched, setSearched] = useState('')
  const [page, setPage] = useState(1)
  const [totalPages, setTotalPages] = useState(1)
  const [loadingMore, setLoadingMore] = useState(false)

  // estado de cada filme (tmdb_id -> convertendo/baixando/...). Deriva do
  // summary compartilhado do cabeçalho (fonte única, atualizado a cada 5s).
  const summary = useJobsSummary()
  // filmes que já vimos "em progresso" e sumiram do summary -> terminaram.
  // Guardamos localmente pois o summary não traz 'done'.
  const finished = useRef<Set<number>>(new Set())
  const prevSummary = useRef<Map<number, MovieState>>(new Map())
  const [states, setStates] = useState<Map<number, MovieState>>(new Map())

  // reduz o summary a um mapa tmdb_id -> state a cada atualização; aplica a
  // regra "sumiu de em-progresso -> done".
  useEffect(() => {
    const cur = new Map<number, MovieState>()
    for (const s of summary) {
      const prev = cur.get(s.tmdb_id)
      // o backend já ordena por prioridade; o 1º de cada tmdb_id vence
      if (prev === undefined) cur.set(s.tmdb_id, s.state)
    }
    // detecta término: estava em progresso no tick anterior e sumiu agora
    for (const [tid, st] of prevSummary.current) {
      if (IN_PROGRESS.has(st) && !cur.has(tid)) finished.current.add(tid)
    }
    // se voltou a aparecer no summary (readicionado), deixa de ser "finished"
    for (const tid of cur.keys()) finished.current.delete(tid)
    prevSummary.current = cur

    const next = new Map(cur)
    for (const tid of finished.current) if (!next.has(tid)) next.set(tid, 'done')
    setStates(next)
  }, [summary])

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

  // nova busca: reseta a lista e carrega a página 1
  async function search(q: string) {
    setMovies(null)
    setError(null)
    setSearched(q)
    try {
      const p = await api<MoviePage>(`/api/movies?q=${encodeURIComponent(q)}&page=1`)
      setMovies(p.results)
      setPage(p.page)
      setTotalPages(p.total_pages)
    } catch (e) {
      setError((e as Error).message)
    }
  }

  // carrega a próxima página e anexa aos resultados atuais
  async function loadMore() {
    if (loadingMore || page >= totalPages) return
    setLoadingMore(true)
    try {
      const next = page + 1
      const p = await api<MoviePage>(`/api/movies?q=${encodeURIComponent(searched)}&page=${next}`)
      // dedup por id (o TMDB às vezes repete títulos entre páginas)
      setMovies((cur) => {
        const seen = new Set((cur ?? []).map((m) => m.id))
        return [...(cur ?? []), ...p.results.filter((m) => !seen.has(m.id))]
      })
      setPage(p.page)
      setTotalPages(p.total_pages)
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setLoadingMore(false)
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
      await post<Job>('/api/jobs', {
        tmdb_id: tmdbId,
        language,
        mode: manual ? 'manual' : 'auto',
        kind,
        // apenas baixar: não há arquivo final, então destino não se aplica
        destination_id: downloadOnly ? null : destId,
        torrent_target_id: targetId,
        download_only: downloadOnly,
        // apenas baixar: nada é convertido, então as opções avançadas não valem
        convert: downloadOnly ? null : advanced,
      })
      // some com a seleção e sinaliza que começou (sem trocar de tela). O
      // estado real chega no próximo tick do summary (≤5s); até lá o overlay
      // "Iniciando..." cobre o intervalo.
      setSelected(null)
      setJustStarted(tmdbId)
      setTimeout(() => setJustStarted((cur) => (cur === tmdbId ? null : cur)), 5000)
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
                {m.in_catalog && !state && (
                  <div className="mt-1.5">
                    <span
                      title="Já existe uma pasta deste filme na coleção"
                      className="inline-flex items-center gap-1 rounded bg-emerald-950 px-1.5 py-0.5 text-xs font-medium text-emerald-300"
                    >
                      <Check width={12} height={12} /> Na coleção
                    </span>
                  </div>
                )}
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

      {movies && movies.length > 0 && page < totalPages && (
        <div className="mt-6 flex justify-center pb-4">
          <button
            onClick={loadMore}
            disabled={loadingMore}
            className="rounded-lg border border-zinc-700 px-5 py-2 text-sm font-medium text-zinc-200 hover:bg-zinc-800 disabled:opacity-50"
          >
            {loadingMore ? 'Carregando…' : `Carregar mais (${page}/${totalPages})`}
          </button>
        </div>
      )}

      {selected && (
        <div
          className="fixed inset-0 z-20 flex items-start justify-center overflow-y-auto bg-black/60 p-4 sm:items-center"
          onClick={() => setSelected(null)}
        >
          <div
            className="w-full max-w-2xl rounded-2xl border border-zinc-700 bg-zinc-900 p-5"
            onClick={(e) => e.stopPropagation()}
          >
            {/* cabeçalho: pôster + título + fechar */}
            <div className="flex items-center gap-3">
              {selected.poster && (
                <img src={selected.poster} className="h-16 w-11 shrink-0 rounded bg-zinc-800 object-cover" alt="" />
              )}
              <div className="min-w-0 flex-1">
                <div className="truncate text-lg font-semibold">{selected.title ?? selected.original_title}</div>
                <div className="text-xs text-zinc-400">
                  {selected.year} {selected.rating ? `· ⭐ ${selected.rating.toFixed(1)}` : ''}
                </div>
              </div>
              <button
                onClick={() => setSelected(null)}
                className="rounded-lg border border-zinc-700 p-1.5 text-zinc-400 hover:text-zinc-200"
                title="Fechar"
              >
                <Xmark width={16} height={16} />
              </button>
            </div>

            {/* Áudio / Destino / Torrents, cada um em 1/3 */}
            <div className="mt-4 flex flex-col gap-3 sm:flex-row">
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
                  {!downloadOnly && <DiskFree disk={destinations.find((d) => d.id === destId)?.disk} />}
                </span>
                {downloadOnly ? (
                  <span className="py-1.5 text-zinc-500" title="Apenas baixar: os arquivos ficam na pasta dos torrents">
                    — (fica na pasta dos torrents)
                  </span>
                ) : destinations.length ? (
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

            {/* opções */}
            <div className="mt-3 flex flex-col gap-1.5">
              <label className="flex items-center gap-1.5 text-sm text-zinc-300">
                <input type="checkbox" checked={manual} onChange={(e) => setManual(e.target.checked)} />
                Escolher torrents manualmente
              </label>
              <label
                className="flex items-center gap-1.5 text-sm text-zinc-300"
                title="Só baixa pelo qBittorrent e conclui — sem conversão, hardlink ou cópia"
              >
                <input type="checkbox" checked={downloadOnly} onChange={(e) => setDownloadOnly(e.target.checked)} />
                Apenas baixar
              </label>
              {downloadOnly && (
                <p className="text-xs text-zinc-500">
                  Os arquivos ficam onde o qBittorrent baixar — sem merge, hardlink ou cópia para o destino.
                </p>
              )}
            </div>

            {/* opções avançadas de conversão (codec/resolução/bitrate/áudios) */}
            <AdvancedOptions
              value={advanced}
              onChange={setAdvanced}
              blocked={downloadOnly
                ? 'Apenas baixar: os arquivos não passam por conversão, então as opções avançadas não se aplicam.'
                : null}
            />

            {/* ações */}
            <div className="mt-4 flex flex-wrap items-center justify-end gap-2">
              <button
                onClick={() => start('original')}
                disabled={starting || (!downloadOnly && !destinations.length)}
                title={downloadOnly ? 'Baixa só o vídeo original' : 'Baixa só o vídeo original, sem merge'}
                className="inline-flex items-center gap-1.5 rounded-lg border border-zinc-600 px-3 py-2 text-sm font-medium text-zinc-200 hover:bg-zinc-800 disabled:opacity-50"
              >
                <MediaVideo width={15} height={15} /> Só original
              </button>
              <button
                onClick={() => start('dubbed')}
                disabled={starting || (!downloadOnly && !destinations.length)}
                title={downloadOnly ? 'Baixa só a versão dublada' : 'Baixa só a versão dublada, sem merge'}
                className="inline-flex items-center gap-1.5 rounded-lg border border-zinc-600 px-3 py-2 text-sm font-medium text-zinc-200 hover:bg-zinc-800 disabled:opacity-50"
              >
                <SoundHigh width={15} height={15} /> Só dublado
              </button>
              <button
                onClick={() => start('both')}
                disabled={starting || (!downloadOnly && !destinations.length)}
                className="inline-flex items-center gap-1.5 rounded-lg bg-blue-600 px-4 py-2 text-sm font-semibold hover:bg-blue-500 disabled:opacity-50"
              >
                <Download width={15} height={15} />
                {downloadOnly ? 'Baixar os dois' : 'Baixar e fazer merge'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
