import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import {
  Calendar, Check, Download, Folder, Hourglass, MediaVideo, NavArrowDown, NavArrowRight,
  SoundHigh, Xmark,
} from 'iconoir-react'
import {
  api, type AdvancedMergeConfig, EPISODE_STATE_LABEL, post,
  type ConvertOptions, type Destination, type EpisodeInfo, type InFlightEpisode, type Job,
  type Language, type Movie, type OwnedFolder, type SeasonDetail, type SeriesDetail,
  type SeriesInProgress, type SeriesOwned, type TorrentTarget,
  type MinQualityByRole, type RetryMode,
} from '../api'
import AdvancedOptions from './AdvancedOptions'
import AdvancedMergePolicy from './AdvancedMergePolicy'
import { useDialog } from './Dialog'
import { DiskFree, Empty, useScrollLock, Toggle } from './ui'
import {
  MinQualityFields, minQualityBody, RetryToggle, ScheduleField, scheduleBody,
  useSearchRetryGlobal,
} from './SearchOptions'

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

/** Como a pasta da coleção casou com a série. */
const MATCH_LABEL: Record<OwnedFolder['match'], string> = {
  tmdbid: 'pelo ID do TMDB',
  title: 'pelo nome',
  loose: 'pelo nome (grafia aproximada)',
}

/** Rótulo do episódio num job ativo: o subestado, ou "aguardando você" quando
 *  o job parou num gate antes de baixar. */
function inFlightLabel(x: InFlightEpisode): string {
  if (x.job_status === 'scheduled') return 'Programado'
  if (x.job_status === 'awaiting' && x.state === 'pending') return 'Aguardando você'
  return EPISODE_STATE_LABEL[x.state] ?? x.state
}

/** Resumo da temporada: "3 baixando" enquanto nenhum passou do download;
 *  depois "em andamento" (convertendo, em revisão...). */
function busySummary(busy: Map<number, InFlightEpisode>): string {
  const all = [...busy.values()]
  if (all.every((x) => x.job_status === 'scheduled')) return `${all.length} programado(s)`
  const downloading = all.every((x) => x.state === 'pending' || x.state === 'downloading')
  return `${all.length} ${downloading ? 'baixando' : 'em andamento'}`
}

const NO_BUSY: Map<number, InFlightEpisode> = new Map()

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
  // política do merge avançado só para este job (null = global)
  const [advMerge, setAdvMerge] = useState<AdvancedMergeConfig | null>(null)
  // manual + pular busca: o usuário já tem os magnets e não quer o indexador
  const [skipSearch, setSkipSearch] = useState(false)
  const [advanced, setAdvanced] = useState<ConvertOptions | null>(null)
  const [starting, setStarting] = useState(false)
  // programação dos já lançados (null = busca já), hora da estreia dos
  // futuros (padrão: a global), busca contínua e qualidade mínima
  const [schedAt, setSchedAt] = useState<string | null>(null)
  const [retryMode, setRetryMode] = useState<RetryMode | null>(null)
  const [minQ, setMinQ] = useState<MinQualityByRole>({})
  const retryGlobal = useSearchRetryGlobal()
  const [releaseTime, setReleaseTime] = useState('')
  useEffect(() => {
    if (retryGlobal && !releaseTime) setReleaseTime(retryGlobal.release_time)
  }, [retryGlobal, releaseTime])
  // o que a coleção já tem (scan dos destinos de séries: episódios + pastas
  // que casaram) — badge informativo; baixar de novo continua permitido (o
  // usuário pode querer trocar a versão)
  const [owned, setOwned] = useState<SeriesOwned | null>(null)
  // episódios em jobs ATIVOS desta série: esses ficam bloqueados (ver abaixo)
  const [inProgress, setInProgress] = useState<SeriesInProgress>({ seasons: {}, done: {} })
  // o usuário escolheu o destino na mão: a pasta encontrada não o sobrescreve
  const [destTouched, setDestTouched] = useState(false)

  // o backend resolve os títulos da série no TMDB (o card só tem o
  // localizado); os daqui são reforço, caso o TMDB não responda
  const loadOwned = useCallback(() => {
    const qs = new URLSearchParams({
      title: series.title ?? '', original_title: series.original_title ?? '',
      year: series.year ?? '',
    })
    return api<SeriesOwned>(`/api/series/${series.id}/owned?${qs}`)
      .then(setOwned)
      .catch(() => {})
  }, [series.id, series.title, series.original_title, series.year])

  useEffect(() => {
    api<SeriesDetail>(`/api/series/${series.id}`)
      .then(setDetail)
      .catch((e) => setError((e as Error).message))
    void loadOwned()
    api<SeriesInProgress>(`/api/series/${series.id}/in-progress`)
      .then(setInProgress)
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
  }, [series.id, loadOwned])

  // a série já tem pasta num destino de séries: os episódios novos vão para
  // lá por padrão (a pasta com o nome do Outstasher primeiro — é nela que o
  // merge entrega), senão nasceria a mesma série em outro destino
  useEffect(() => {
    if (destTouched || !owned?.folders.length) return
    const known = owned.folders.filter((f) => destinations.some((d) => d.id === f.destination_id))
    const pref = known.find((f) => f.folder === owned.canonical_name) ?? known[0]
    if (pref) setDestId(pref.destination_id)
  }, [owned, destinations, destTouched])

  // na coleção = scan do destino + o que jobs ainda ativos já entregaram (o
  // cache do scan só é invalidado quando o job inteiro termina)
  const ownedBySeason = useMemo(() => {
    const m = new Map<number, Set<number>>()
    for (const rec of [owned?.seasons ?? {}, inProgress.done]) {
      for (const [s, eps] of Object.entries(rec)) {
        const set = m.get(Number(s)) ?? new Set<number>()
        for (const e of eps) set.add(e)
        m.set(Number(s), set)
      }
    }
    return m
  }, [owned, inProgress])

  // Episódio em job ativo: desmarcado e BLOQUEADO (não só sinalizado). Pedir
  // de novo cria um segundo job escrevendo o mesmo arquivo final — e o
  // cancelamento de um apaga a entrega do outro (caso real). Quem quer
  // refazer resolve/cancela o job existente (o badge leva até ele).
  const busyBySeason = useMemo(() => {
    const m = new Map<number, Map<number, InFlightEpisode>>()
    for (const [s, eps] of Object.entries(inProgress.seasons)) {
      m.set(Number(s), new Map(eps.map((x) => [x.episode, x])))
    }
    return m
  }, [inProgress])
  const busyOf = (season: number) => busyBySeason.get(season) ?? NO_BUSY
  const busyJobs = useMemo(() => {
    const ids = new Set<string>()
    let n = 0
    for (const eps of Object.values(inProgress.seasons)) {
      for (const x of eps) { ids.add(x.job_id); n++ }
    }
    return { ids: [...ids], count: n }
  }, [inProgress])

  /** Episódios da temporada (do estado ou buscando agora). null = falhou. */
  async function loadEpisodes(season: number): Promise<EpisodeInfo[] | null> {
    const cur = episodes.get(season)
    if (Array.isArray(cur)) return cur
    setEpisodes((m) => new Map(m).set(season, 'loading'))
    try {
      const d = await api<SeasonDetail>(`/api/series/${series.id}/season/${season}`)
      setEpisodes((m) => new Map(m).set(season, d.episodes))
      return d.episodes
    } catch {
      setEpisodes((m) => {
        const n = new Map(m)
        n.delete(season)
        return n
      })
      return null
    }
  }

  function toggleOpen(season: number) {
    const next = new Set(open)
    if (next.has(season)) {
      next.delete(season)
      setOpen(next)
      return
    }
    next.add(season)
    setOpen(next)
    if (!episodes.has(season)) void loadEpisodes(season)
  }

  /** O que dá para pedir na temporada: fora de jobs ativos. O que ainda
   *  não estreou vira job programado para o dia do lançamento (um por data). */
  function selectableOf(season: number, eps: EpisodeInfo[]): number[] {
    const busy = busyOf(season)
    return eps.filter((e) => !busy.has(e.episode)).map((e) => e.episode)
  }

  /** Temporada toda marcada: inteira, ou todos os pedíveis um a um (a que
   *  tem episódio em job ativo só pode ser marcada assim). */
  function seasonFullyPicked(season: number): boolean {
    if (pickedSeasons.has(season)) return true
    const eps = episodes.get(season)
    const picked = pickedEpisodes.get(season)
    if (!Array.isArray(eps) || !picked) return false
    const sel = selectableOf(season, eps)
    return sel.length > 0 && sel.every((e) => picked.has(e))
  }

  async function toggleSeason(season: number) {
    const clear = () => {
      setPickedSeasons((cur) => {
        const next = new Set(cur)
        next.delete(season)
        return next
      })
      // marcar/desmarcar a temporada inteira descarta a seleção parcial dela
      setPickedEpisodes((cur) => {
        const next = new Map(cur)
        next.delete(season)
        return next
      })
    }
    if (seasonFullyPicked(season)) {
      clear()
      return
    }
    if (busyOf(season).size === 0) {
      clear()
      setPickedSeasons((cur) => new Set(cur).add(season))
      return
    }
    // "temporada inteira" pediria de novo o que o job ativo está baixando:
    // vira a lista explícita sem esses episódios
    const eps = await loadEpisodes(season)
    if (!eps) return
    const sel = selectableOf(season, eps)
    clear()
    if (sel.length) setPickedEpisodes((cur) => new Map(cur).set(season, new Set(sel)))
  }

  function toggleEpisode(season: number, ep: number) {
    const eps = episodes.get(season)
    if (!Array.isArray(eps) || busyOf(season).has(ep)) return
    setPickedEpisodes((cur) => {
      const next = new Map(cur)
      // temporada inteira marcada + desmarcar um episódio = vira seleção
      // explícita com todos menos ele (só os já lançados e fora de jobs ativos)
      const base = pickedSeasons.has(season)
        ? new Set(selectableOf(season, eps))
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
    if (busyOf(season).has(ep)) return false
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

  /** A seleção sem os episódios em jobs ativos — cobre a temporada marcada
   *  inteira ANTES de o aviso de "baixando" chegar. */
  async function requestable(): Promise<SeriesSelection> {
    const seasons: number[] = []
    const eps: Record<number, number[]> = {}
    for (const [s, list] of Object.entries(selection.episodes)) {
      const busy = busyOf(Number(s))
      const keep = list.filter((e) => !busy.has(e))
      if (keep.length) eps[Number(s)] = keep
    }
    for (const s of selection.seasons) {
      if (busyOf(s).size === 0) {
        seasons.push(s)
        continue
      }
      const list = await loadEpisodes(s)
      if (!list) throw new Error(`Não deu para carregar os episódios da temporada ${s}`)
      const sel = selectableOf(s, list)
      if (sel.length) eps[s] = sel
    }
    return { seasons, episodes: eps }
  }

  // pasta que casou por nome (ou com nome fora do padrão): os episódios novos
  // já caem NELA quando está no destino do job; renomear para o nome do
  // Outstasher só a deixa no padrão e marcada com o id — ação explícita,
  // nunca automática (outro programa pode depender do caminho)
  function canStandardize(f: OwnedFolder): boolean {
    const canonical = owned?.canonical_name
    if (!canonical || !detail?.original_title || f.folder === canonical) return false
    return !owned.folders.some((o) => o.destination_id === f.destination_id && o.folder === canonical)
  }

  async function standardize(f: OwnedFolder) {
    const canonical = owned?.canonical_name
    if (!canonical || !detail) return
    if (!(await dialog.confirm({
      title: 'Padronizar a pasta da série',
      message: `Renomear "${f.folder}" para "${canonical}"? O Jellyfin passa a `
        + 'identificá-la pelo ID do TMDB, sem depender do título. Os episódios novos '
        + 'vão para esta pasta com ou sem o nome novo, e os arquivos dentro dela não '
        + 'mudam. Se outro programa gerencia esta pasta, ele perde o caminho.',
      confirmText: 'Renomear',
    }))) return
    try {
      await post('/api/catalog/item/tmdbid', {
        folder: f.folder, destination_id: f.destination_id, tmdb_id: series.id,
        title: detail.original_title, year: detail.year,
      })
      await loadOwned()
    } catch (e) {
      await dialog.alert({ title: 'Erro ao renomear', message: (e as Error).message })
    }
  }

  // mesmo `kind` dos filmes: os dois lados com merge, ou um lado só entregue direto
  async function start(kind: 'both' | 'original' | 'dubbed') {
    if (!hasSelection || starting) return
    setStarting(true)
    try {
      const sel = await requestable()
      if (!sel.seasons.length && !Object.keys(sel.episodes).length) {
        await dialog.alert({
          title: 'Nada a pedir',
          message: 'Os episódios marcados já estão em jobs ativos desta série.',
        })
        return
      }
      const job = await post<Job>('/api/jobs/series', {
        tmdb_id: series.id,
        language,
        seasons: sel.seasons,
        episodes: sel.episodes,
        mode: manual ? 'manual' : 'auto',
        skip_search: manual && skipSearch,
        // um lado só não passa por merge: a política do merge avançado não vale
        advanced_merge: kind === 'both' ? advMerge : null,
        kind,
        destination_id: destId,
        torrent_target_id: targetId,
        convert: advanced,
        scheduled_at: scheduleBody(schedAt),
        retry_mode: retryMode,
        min_quality: manual ? null : minQualityBody(minQ,
          kind === 'both' ? ['video', 'audio'] : kind === 'original' ? ['video'] : ['audio']),
        // episódio futuro vira job programado para o dia do lançamento
        schedule_future: true,
        release_time: releaseTime || null,
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
          {/* pastas da coleção que são esta série — inclusive as de outros
              programas, achadas pelo nome */}
          {owned && owned.folders.length > 0 && (
            <div className="mb-2 flex flex-col gap-1">
              {owned.folders.map((f) => (
                <div
                  key={`${f.destination_id}:${f.folder}`}
                  className="flex items-center gap-2 rounded-lg border border-zinc-800 px-3 py-1.5 text-xs text-zinc-400"
                >
                  <Folder width={13} height={13} className="shrink-0 text-emerald-400" />
                  <span className="min-w-0 flex-1 truncate" title={f.folder}>
                    <span className="text-zinc-200">{f.folder}</span>
                    {' · '}{f.destination_label} · {f.episodes} ep. · {MATCH_LABEL[f.match]}
                  </span>
                  {canStandardize(f) && (
                    <button
                      onClick={() => standardize(f)}
                      title={`Renomear para "${owned.canonical_name}"`}
                      className="shrink-0 rounded border border-zinc-700 px-1.5 py-0.5 text-zinc-300 hover:border-blue-700 hover:text-blue-300"
                    >
                      Padronizar nome
                    </button>
                  )}
                </div>
              ))}
            </div>
          )}
          {busyJobs.count > 0 && (
            <div className="mb-2 rounded-lg border border-amber-900/60 bg-amber-950/30 px-3 py-2 text-xs text-amber-200">
              {busyJobs.count} episódio(s) desta série já estão em{' '}
              {busyJobs.ids.length > 1 ? `${busyJobs.ids.length} jobs ativos` : 'um job ativo'} e
              ficam bloqueados — pedir de novo duplicaria o download e o arquivo final.
              Acompanhe em{' '}
              {busyJobs.ids.map((id, i) => (
                <span key={id}>
                  {i > 0 && ', '}
                  <Link to={`/jobs/${id}`} className="underline hover:text-amber-100">{id}</Link>
                </span>
              ))}.
            </div>
          )}
          {error && <Empty>Erro: {error}</Empty>}
          {!detail && !error && <Empty>Carregando...</Empty>}
          {detail && (
            <div className="flex max-h-80 flex-col gap-1 overflow-y-auto pr-1">
              {detail.seasons.map((s) => {
                const eps = episodes.get(s.season)
                const isOpen = open.has(s.season)
                const ownedHere = ownedBySeason.get(s.season)
                const busy = busyOf(s.season)
                const full = seasonFullyPicked(s.season)
                const partial = !full && (pickedEpisodes.get(s.season)?.size ?? 0) > 0
                return (
                  <div key={s.season} className="rounded-lg border border-zinc-800">
                    <div className="flex items-center gap-2 px-3 py-2">
                      <input
                        type="checkbox"
                        checked={full}
                        // seleção parcial: o estado visual fica no contador ao lado
                        onChange={() => void toggleSeason(s.season)}
                        title={busy.size > 0
                          ? 'Temporada inteira, menos os episódios em jobs ativos'
                          : 'Temporada inteira'}
                      />
                      <button
                        onClick={() => toggleOpen(s.season)}
                        className="flex min-w-0 flex-1 flex-wrap items-center gap-1.5 text-left text-sm text-zinc-200 hover:text-white"
                      >
                        {isOpen
                          ? <NavArrowDown width={14} height={14} className="shrink-0 text-zinc-500" />
                          : <NavArrowRight width={14} height={14} className="shrink-0 text-zinc-500" />}
                        <span className="truncate font-medium">{s.name ?? `Temporada ${s.season}`}</span>
                        <span className="shrink-0 text-xs text-zinc-500">
                          · {s.episode_count ?? '?'} ep. {s.air_date ? `· ${s.air_date.slice(0, 4)}` : ''}
                        </span>
                        {ownedHere && ownedHere.size > 0 && (
                          <span
                            className="shrink-0 rounded bg-emerald-950 px-1.5 py-0.5 text-xs font-medium text-emerald-300"
                            title="Episódios desta temporada já na coleção"
                          >
                            {ownedHere.size}/{s.episode_count ?? '?'} na coleção
                          </span>
                        )}
                        {busy.size > 0 && (
                          <span
                            className="shrink-0 rounded bg-amber-950 px-1.5 py-0.5 text-xs font-medium text-amber-300"
                            title="Episódios desta temporada em jobs ativos — ficam bloqueados"
                          >
                            {busySummary(busy)}
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
                              const job = busy.get(e.episode)
                              const blocked = job !== undefined
                              return (
                                <li key={e.episode}>
                                  <label
                                    className={`flex items-center gap-2 rounded px-1 py-0.5 text-sm ${
                                      job ? '' : 'hover:bg-zinc-800/60'
                                    }`}
                                    title={future && !job ? 'Ainda não lançado — vira um job programado que busca no dia da estreia'
                                      : job ? 'Já está num job ativo — pedir de novo duplicaria o download'
                                        : e.overview ?? undefined}
                                  >
                                    <input
                                      type="checkbox"
                                      disabled={blocked}
                                      checked={!blocked && isEpisodePicked(s.season, e.episode)}
                                      onChange={() => toggleEpisode(s.season, e.episode)}
                                    />
                                    <span className="w-10 shrink-0 font-mono text-xs text-zinc-500">
                                      E{String(e.episode).padStart(2, '0')}
                                    </span>
                                    <span className="min-w-0 flex-1 truncate text-zinc-300">
                                      {e.name ?? '—'}
                                      {ownedHere?.has(e.episode) && (
                                        <span
                                          className="ml-1.5 inline-flex items-center gap-0.5 rounded bg-emerald-950 px-1 py-px text-[10px] font-medium text-emerald-300"
                                          title="Já existe na coleção"
                                        >
                                          <Check width={9} height={9} /> na coleção
                                        </span>
                                      )}
                                      {job && (
                                        <Link
                                          to={`/jobs/${job.job_id}`}
                                          className={`ml-1.5 inline-flex items-center gap-0.5 rounded px-1 py-px text-[10px] font-medium ${
                                            job.job_status === 'scheduled'
                                              ? 'bg-teal-950 text-teal-300 hover:text-teal-100'
                                              : 'bg-amber-950 text-amber-300 hover:text-amber-100'}`}
                                          title={`Num job ativo (${job.job_id}) — abrir o job`}
                                        >
                                          {job.job_status === 'scheduled'
                                            ? <Calendar width={9} height={9} />
                                            : <Hourglass width={9} height={9} />} {inFlightLabel(job)}
                                        </Link>
                                      )}
                                    </span>
                                    <span className={`flex shrink-0 items-center gap-1 text-xs ${future ? 'text-teal-400' : 'text-zinc-500'}`}>
                                      {future && <Calendar width={11} height={11} />}
                                      {future ? (e.air_date ? `estreia em ${fmtAirDate(e.air_date)}` : 'sem data de estreia') : fmtAirDate(e.air_date)}
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
                onChange={(e) => {
                  setDestTouched(true)
                  setDestId(Number(e.target.value))
                }}
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
            <Toggle checked={manual} onChange={(v) => setManual(v)} />
            Escolher torrents manualmente (por episódio)
          </label>
          {manual && (
            <label
              className="ml-5 flex items-center gap-1.5 text-sm text-zinc-300"
              title="Não consulta o indexador: você cola o magnet/link de cada torrent"
            >
              <Toggle checked={skipSearch}
                onChange={(v) => setSkipSearch(v)} />
              Pular busca de torrents
            </label>
          )}
          {manual && skipSearch && (
            <p className="ml-5 text-xs text-zinc-500">
              O job abre direto na escolha, com a lista vazia — informe lá o
              magnet ou link .torrent de cada torrent e a que episódios ele serve.
            </p>
          )}
          <ScheduleField value={schedAt} onChange={setSchedAt}
            label="Agendar download (episódios já lançados)" />
          <label className="flex flex-wrap items-center gap-1.5 text-sm text-zinc-300"
            title="Cada data de estreia vira um job programado; ele começa a procurar nesse dia, a partir desta hora">
            <Calendar width={14} height={14} className="text-teal-400" />
            Episódios futuros: buscar no dia da estreia a partir das
            <input type="time" value={releaseTime}
              onChange={(e) => setReleaseTime(e.target.value)}
              className="rounded-lg border border-zinc-700 bg-zinc-800 px-2 py-0.5 text-sm" />
          </label>
          <RetryToggle mode={retryMode} onChange={setRetryMode} global={retryGlobal} />
          {!manual && (
            <div className="mt-1 rounded-lg border border-zinc-800 p-2.5">
              <MinQualityFields value={minQ} onChange={setMinQ} />
            </div>
          )}
        </div>

        {/* opções avançadas de conversão (aplicadas episódio a episódio) */}
        <AdvancedOptions value={advanced} onChange={setAdvanced} />

        {/* se algum episódio precisar do alinhamento por conteúdo */}
        <div className="mt-3">
          <AdvancedMergePolicy value={advMerge} onChange={setAdvMerge} title="Se precisar do merge avançado (montagens diferentes)" />
        </div>

        {/* ações */}
        <div className="mt-4 flex items-center justify-end gap-2">
          {!hasSelection && (
            <span className="mr-auto min-w-0 truncate text-xs text-zinc-500">Selecione temporadas ou episódios.</span>
          )}
          <button
            onClick={() => start('original')}
            disabled={!hasSelection || starting || !destinations.length}
            title="Baixa só a versão original de cada episódio, sem merge"
            className="inline-flex shrink-0 items-center gap-1.5 whitespace-nowrap rounded-lg border border-zinc-600 px-3 py-2 text-sm font-medium text-zinc-200 hover:bg-zinc-800 disabled:opacity-50"
          >
            <MediaVideo width={15} height={15} /> Só original
          </button>
          <button
            onClick={() => start('dubbed')}
            disabled={!hasSelection || starting || !destinations.length}
            title="Baixa só a versão dublada de cada episódio, sem merge"
            className="inline-flex shrink-0 items-center gap-1.5 whitespace-nowrap rounded-lg border border-zinc-600 px-3 py-2 text-sm font-medium text-zinc-200 hover:bg-zinc-800 disabled:opacity-50"
          >
            <SoundHigh width={15} height={15} /> Só dublado
          </button>
          <button
            onClick={() => start('both')}
            disabled={!hasSelection || starting || !destinations.length}
            title="Baixa as versões original e dublada de cada episódio e faz o merge"
            className="inline-flex shrink-0 items-center gap-1.5 whitespace-nowrap rounded-lg bg-blue-600 px-4 py-2 text-sm font-semibold hover:bg-blue-500 disabled:opacity-50"
          >
            <Download width={15} height={15} /> Baixar e fazer merge
          </button>
        </div>
      </div>
    </div>
  )
}
