import { useEffect, useMemo, useState } from 'react'
import { Calendar, NavArrowDown, NavArrowRight, Xmark } from 'iconoir-react'
import {
  api, type EpisodeInfo, type Movie, type SeasonDetail, type SeriesDetail,
} from '../api'
import { Empty, useScrollLock } from './ui'

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

/** Modal de série: escolha de temporada(s) e episódio(s) com data de estreia.
 *  O disparo do download chega com o pipeline de séries; até lá o rodapé
 *  informa e o botão fica desabilitado. */
export default function SeriesModal({
  series,
  onClose,
}: {
  series: Movie
  onClose: () => void
}) {
  useScrollLock(true)
  const [detail, setDetail] = useState<SeriesDetail | null>(null)
  const [error, setError] = useState<string | null>(null)
  // temporadas expandidas e os episódios já carregados (lazy, por temporada)
  const [open, setOpen] = useState<Set<number>>(new Set())
  const [episodes, setEpisodes] = useState<Map<number, EpisodeInfo[] | 'loading'>>(new Map())
  // temporadas inteiras marcadas + episódios avulsos de temporadas parciais
  const [pickedSeasons, setPickedSeasons] = useState<Set<number>>(new Set())
  const [pickedEpisodes, setPickedEpisodes] = useState<Map<number, Set<number>>>(new Map())

  useEffect(() => {
    api<SeriesDetail>(`/api/series/${series.id}`)
      .then(setDetail)
      .catch((e) => setError((e as Error).message))
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
      // explícita com todos menos ele
      const base = pickedSeasons.has(season)
        ? new Set(eps.map((e) => e.episode))
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
            <div className="flex flex-col gap-1">
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

        {/* rodapé: o disparo do job chega com o pipeline de séries (em obra) */}
        <div className="mt-4 flex flex-wrap items-center justify-end gap-2">
          <span className="mr-auto text-xs text-zinc-500">
            O download de séries está em desenvolvimento — por enquanto dá para explorar e selecionar.
          </span>
          <button
            disabled
            title="Em breve"
            className="rounded-lg bg-blue-600 px-4 py-2 text-sm font-semibold opacity-50"
          >
            {hasSelection ? 'Baixar seleção' : 'Selecione temporadas ou episódios'}
          </button>
        </div>
      </div>
    </div>
  )
}
