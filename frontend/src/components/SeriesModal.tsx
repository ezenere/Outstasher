import { useEffect, useMemo, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { Calendar, Check, Download, NavArrowDown, NavArrowRight, Xmark } from 'iconoir-react'
import {
  api, post,
  type ConvertOptions, type Destination, type EpisodeInfo, type Job,
  type Language, type Movie, type SeasonDetail, type SeriesDetail,
  type TorrentTarget,
} from '../api'
import AdvancedOptions from './AdvancedOptions'
import { useDialog } from './Dialog'
import { DiskFree, Empty, useScrollLock } from './ui'

/** Seleção do modal: temporadas inteiras + episódios avulsos por temporada.
 *  É o shape que o job de série recebe (request.seasons / request.episodes). */
export interface SeriesSelection {
  seasons: number[]
  episodes: Record<number, number[]>
}

/** Data de estreia no futuro (ou ausente) = episódio ainda não lançado. */
export function isFutureEpisode(airDate: string | null): boolean {
  if (!airDate) return true
  // comparação de datas ISO (YYYY-MM-DD) como string funciona lexicograficamente
  return airDate > new Date().toISOString().slice(0, 10)
}

function fmtAirDate(d: string | null): string {
  if (!d) return 'sem data'
  const [y, m, day] = d.split('-')
  return `${day}/${m}/${y}`
}

/** Modal de série: escolha de temporada(s)/episódio(s) + idioma/destino e
 *  disparo do job (sempre original + dublado de cada episódio). */
export default function SeriesModal({
  series,
  onClose,
}: {
  series: Movie
  onClose: () => void
}) {
  useScrollLock(true)
  const dialog = useDialog()
  const navigate = useNavigate()
  const [detail, setDetail] = useState<SeriesDetail | null>(null)
  const [error, setError] = useState<string | null>(null)
  // temporadas expandidas e os episódios já carregados (lazy, por temporada)
  const [open, setOpen] = useState<Set<number>>(new Set())
  const [episodes, setEpisodes] = useState<Map<number, EpisodeInfo[] | 'loading'>>(new Map())
  // temporadas inteiras marcadas + episódios avulsos de temporadas parciais
  const [pickedSeasons, setPickedSeasons] = useState<Set<number>>(new Set())
  const [pickedEpisodes, setPickedEpisodes] = useState<Map<number, Set<number>>>(new Map())
  // idioma/destino/torrents/modo — mesmos cadastros do modal de filmes
  const [languages, setLanguages] = useState<Language[]>([])
  const [language, setLanguage] = useState('pt')
  const [destinations, setDestinations] = useState<Destination[]>([])
  const [destId, setDestId] = useState<number | null>(null)
  const [targets, setTargets] = useState<TorrentTarget[]>([])
  const [targetId, setTargetId] = useState<number | null>(null)
  const [manual, setManual] = useState(false)
  // manual + pular busca: o usuário já tem os magnets e não quer o indexador
  const [skipSearch, setSkipSearch] = useState(false)
  const [advanced, setAdvanced] = useState<ConvertOptions | null>(null)
  const [starting, setStarting] = useState(false)
  // episódios já na coleção ({temporada: [eps]}) — badge informativo; baixar
  // de novo continua permitido (o usuário pode querer trocar a versão)
  const [owned, setOwned] = useState<Record<number, number[]>>({})

  useEffect(() => {
    api<SeriesDetail>(`/api/series/${series.id}`)
      .then(setDetail)
      .catch((e) => setError((e as Error).message))
    api<{ seasons: Record<number, number[]> }>(
      `/api/series/${series.id}/owned?title=${encodeURIComponent(series.title ?? '')}`
      + `&year=${encodeURIComponent(series.year ?? '')}`)
      .then((r) => setOwned(r.seasons))
      .catch(() => {})
    api<Language[]>('/api/languages').then(setLanguages).catch(() => {})
    api<Destination[]>('/api/destinations?media=tv')
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
  }, [series.id])

  async function toggleOpen(season: number) {
    const next = new Set(open)
    if (next.has(season)) {
      next.delete(season)
      setOpen(next)
      return
    }
    next.add(season)
    setOpen(next)
    if (!episodes.has(season)) {
      setEpisodes((cur) => new Map(cur).set(season, 'loading'))
      try {
        const d = await api<SeasonDetail>(`/api/series/${series.id}/season/${season}`)
        setEpisodes((cur) => new Map(cur).set(season, d.episodes))
      } catch {
        setEpisodes((cur) => {
          const m = new Map(cur)
          m.delete(season)
          return m
        })
      }
    }
  }

  function toggleSeason(season: number) {
    setPickedSeasons((cur) => {
      const next = new Set(cur)
      if (next.has(season)) next.delete(season)
      else next.add(season)
      return next
    })
    // marcar/desmarcar a temporada inteira descarta a seleção parcial dela
    setPickedEpisodes((cur) => {
      const next = new Map(cur)
      next.delete(season)
      return next
    })
  }

  function toggleEpisode(season: number, ep: number) {
    const eps = episodes.get(season)
    if (!Array.isArray(eps)) return
    setPickedEpisodes((cur) => {
      const next = new Map(cur)
      // temporada inteira marcada + desmarcar um episódio = vira seleção
      // explícita com todos menos ele (só os já lançados)
      const base = pickedSeasons.has(season)
        ? new Set(eps.filter((e) => !isFutureEpisode(e.air_date)).map((e) => e.episode))
        : new Set(next.get(season) ?? [])
      if (base.has(ep)) base.delete(ep)
      else base.add(ep)
      if (base.size === 0) next.delete(season)
      else next.set(season, base)
      return next
    })
    setPickedSeasons((cur) => {
      const next = new Set(cur)
      next.delete(season)
      return next
    })
  }

  function isEpisodePicked(season: number, ep: number): boolean {
    if (pickedSeasons.has(season)) return true
    return pickedEpisodes.get(season)?.has(ep) ?? false
  }

  const selection: SeriesSelection = useMemo(() => ({
    seasons: [...pickedSeasons].sort((a, b) => a - b),
    episodes: Object.fromEntries(
      [...pickedEpisodes.entries()].map(([s, eps]) => [s, [...eps].sort((a, b) => a - b)]),
    ),
  }), [pickedSeasons, pickedEpisodes])

  const hasSelection = selection.seasons.length > 0 || Object.keys(selection.episodes).length > 0

  async function start() {
    if (!hasSelection || starting) return
    setStarting(true)
    try {
      const job = await post<Job>('/api/jobs/series', {
        tmdb_id: series.id,
        language,
        seasons: selection.seasons,
        episodes: selection.episodes,
        mode: manual ? 'manual' : 'auto',
        skip_search: manual && skipSearch,
        destination_id: destId,
        torrent_target_id: targetId,
        convert: advanced,
      })
      onClose()
      navigate(`/jobs/${job.id}`)
    } catch (e) {
      await dialog.alert({ title: 'Erro ao criar job', message: (e as Error).message })
    } finally {
      setStarting(false)
    }
  }

  return (
    <div
      className="fixed inset-0 z-20 flex justify-center overflow-y-auto bg-black/60 p-4"
      onClick={onClose}
    >
      {/* my-auto (e não items-center): centraliza quando cabe, cresce para
          baixo quando o conteúdo passa da tela (mesmo padrão dos outros modais) */}
      <div
        className="my-auto h-fit w-full max-w-2xl rounded-2xl border border-zinc-700 bg-zinc-900 p-5"
        onClick={(e) => e.stopPropagation()}
      >
        {/* cabeçalho: pôster + título + fechar */}
        <div className="flex items-center gap-3">
          {series.poster && (
            <img src={series.poster} className="h-16 w-11 shrink-0 rounded bg-zinc-800 object-cover" alt="" />
          )}
          <div className="min-w-0 flex-1">
            <div className="truncate text-lg font-semibold">{series.title ?? series.original_title}</div>
            <div className="text-xs text-zinc-400">
              Série · {series.year} {series.rating ? `· ⭐ ${series.rating.toFixed(1)}` : ''}
            </div>
          </div>
          <button
            onClick={onClose}
            className="rounded-lg border border-zinc-700 p-1.5 text-zinc-400 hover:text-zinc-200"
            title="Fechar"
          >
            <Xmark width={16} height={16} />
          </button>
        </div>

        {/* temporadas + episódios */}
        <div className="mt-4">
          <div className="mb-2 text-sm text-zinc-400">Temporadas e episódios</div>
          {error && <Empty>Erro: {error}</Empty>}
          {!detail && !error && <Empty>Carregando...</Empty>}
          {detail && (
            <div className="flex max-h-80 flex-col gap-1 overflow-y-auto pr-1">
              {detail.seasons.map((s) => {
                const eps = episodes.get(s.season)
                const isOpen = open.has(s.season)
                const partial = (pickedEpisodes.get(s.season)?.size ?? 0) > 0
                return (
                  <div key={s.season} className="rounded-lg border border-zinc-800">
                    <div className="flex items-center gap-2 px-3 py-2">
                      <input
                        type="checkbox"
                        checked={pickedSeasons.has(s.season)}
                        // seleção parcial: o estado visual fica no contador ao lado
                        onChange={() => toggleSeason(s.season)}
                        title="Temporada inteira"
                      />
                      <button
                        onClick={() => toggleOpen(s.season)}
                        className="flex min-w-0 flex-1 items-center gap-1.5 text-left text-sm text-zinc-200 hover:text-white"
                      >
                        {isOpen
                          ? <NavArrowDown width={14} height={14} className="shrink-0 text-zinc-500" />
                          : <NavArrowRight width={14} height={14} className="shrink-0 text-zinc-500" />}
                        <span className="truncate font-medium">{s.name ?? `Temporada ${s.season}`}</span>
                        <span className="shrink-0 text-xs text-zinc-500">
                          · {s.episode_count ?? '?'} ep. {s.air_date ? `· ${s.air_date.slice(0, 4)}` : ''}
                        </span>
                        {(owned[s.season]?.length ?? 0) > 0 && (
                          <span
                            className="shrink-0 rounded bg-emerald-950 px-1.5 py-0.5 text-xs font-medium text-emerald-300"
                            title="Episódios desta temporada já na coleção"
                          >
                            {owned[s.season]!.length}/{s.episode_count ?? '?'} na coleção
                          </span>
                        )}
                      </button>
                      {partial && (
                        <span className="shrink-0 rounded bg-blue-950 px-1.5 py-0.5 text-xs font-medium text-blue-300">
                          {pickedEpisodes.get(s.season)!.size} selec.
                        </span>
                      )}
                    </div>
                    {isOpen && (
                      <div className="border-t border-zinc-800 px-3 py-2">
                        {eps === 'loading' || eps === undefined ? (
                          <div className="py-1 text-sm text-zinc-500">Carregando episódios...</div>
                        ) : (
                          <ul className="flex flex-col gap-1">
                            {eps.map((e) => {
                              const future = isFutureEpisode(e.air_date)
                              return (
                                <li key={e.episode}>
                                  <label
                                    className={`flex items-center gap-2 rounded px-1 py-0.5 text-sm ${
                                      future ? 'opacity-50' : 'hover:bg-zinc-800/60'
                                    }`}
                                    title={future ? 'Ainda não lançado' : e.overview ?? undefined}
                                  >
                                    <input
                                      type="checkbox"
                                      disabled={future}
                                      checked={!future && isEpisodePicked(s.season, e.episode)}
                                      onChange={() => toggleEpisode(s.season, e.episode)}
                                    />
                                    <span className="w-10 shrink-0 font-mono text-xs text-zinc-500">
                                      E{String(e.episode).padStart(2, '0')}
                                    </span>
                                    <span className="min-w-0 flex-1 truncate text-zinc-300">
                                      {e.name ?? '—'}
                                      {owned[s.season]?.includes(e.episode) && (
                                        <span
                                          className="ml-1.5 inline-flex items-center gap-0.5 rounded bg-emerald-950 px-1 py-px text-[10px] font-medium text-emerald-300"
                                          title="Já existe na coleção"
                                        >
                                          <Check width={9} height={9} /> na coleção
                                        </span>
                                      )}
                                    </span>
                                    <span className="flex shrink-0 items-center gap-1 text-xs text-zinc-500">
                                      {future && <Calendar width={11} height={11} />}
                                      {future ? `estreia em ${fmtAirDate(e.air_date)}` : fmtAirDate(e.air_date)}
                                      {e.runtime ? ` · ${e.runtime}min` : ''}
                                    </span>
                                  </label>
                                </li>
                              )
                            })}
                          </ul>
                        )}
                      </div>
                    )}
                  </div>
                )
              })}
            </div>
          )}
        </div>

        {/* Áudio / Destino / Torrents — mesmo layout do modal de filmes */}
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

        {/* opções */}
        <div className="mt-3 flex flex-col gap-1.5">
          <label className="flex items-center gap-1.5 text-sm text-zinc-300">
            <input type="checkbox" checked={manual} onChange={(e) => setManual(e.target.checked)} />
            Escolher torrents manualmente (por episódio)
          </label>
          {manual && (
            <label
              className="ml-5 flex items-center gap-1.5 text-sm text-zinc-300"
              title="Não consulta o indexador: você cola o magnet/link de cada torrent"
            >
              <input type="checkbox" checked={skipSearch}
                onChange={(e) => setSkipSearch(e.target.checked)} />
              Pular busca de torrents
            </label>
          )}
          {manual && skipSearch && (
            <p className="ml-5 text-xs text-zinc-500">
              O job abre direto na escolha, com a lista vazia — informe lá o
              magnet ou link .torrent de cada torrent e a que episódios ele serve.
            </p>
          )}
        </div>

        {/* opções avançadas de conversão (aplicadas episódio a episódio) */}
        <AdvancedOptions value={advanced} onChange={setAdvanced} />

        {/* ações */}
        <div className="mt-4 flex flex-wrap items-center justify-end gap-2">
          <span className="mr-auto text-xs text-zinc-500">
            Baixa as versões original e dublada de cada episódio selecionado.
          </span>
          <button
            onClick={start}
            disabled={!hasSelection || starting || !destinations.length}
            className="inline-flex items-center gap-1.5 rounded-lg bg-blue-600 px-4 py-2 text-sm font-semibold hover:bg-blue-500 disabled:opacity-50"
          >
            <Download width={15} height={15} />
            {hasSelection ? 'Baixar seleção' : 'Selecione temporadas ou episódios'}
          </button>
        </div>
      </div>
    </div>
  )
}
