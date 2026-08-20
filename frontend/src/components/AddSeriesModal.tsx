import { useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  Folder, MediaVideo, Play, Search, SoundHigh, Tv, Xmark,
} from 'iconoir-react'
import {
  api, post, type ConvertOptions, type Destination, type Job, type Language,
  type ManualRow, type ManualScan, type ManualSeason, type Movie, type MoviePage,
} from '../api'
import AdvancedOptions from './AdvancedOptions'
import FolderPicker from './FolderPicker'
import { useScrollLock } from './ui'

interface Props {
  destinations: Destination[]
  defaultDestId: number | null
  onClose: () => void
}

const fmtDur = (s: number | null) =>
  s == null ? '—' : `${Math.floor(s / 60)}min`

/** Merge manual de SÉRIE: os arquivos já estão no disco.
 *
 *  O problema que só série tem é a NUMERAÇÃO: cada lado pode nomear os
 *  episódios de um jeito (SxxEyy, absoluta, nenhum) e um arquivo pode conter
 *  dois episódios. Por isso a tela mostra a ordem detectada de cada lado e
 *  deixa trocar o pareamento linha a linha antes de converter. */
export default function AddSeriesModal({ destinations, defaultDestId, onClose }: Props) {
  const [languages, setLanguages] = useState<Language[]>([])
  const [language, setLanguage] = useState('pt')
  const [query, setQuery] = useState('')
  const [results, setResults] = useState<Movie[] | null>(null)
  const [searching, setSearching] = useState(false)
  const [series, setSeries] = useState<Movie | null>(null)
  const [origRoot, setOrigRoot] = useState('')
  const [dubRoot, setDubRoot] = useState('')
  const [picking, setPicking] = useState<null | { side: 'orig' | 'dub' }>(null)
  const [pickFile, setPickFile] = useState<null | { season: number; ep: number; side: 'orig' | 'dub' }>(null)
  const [scan, setScan] = useState<ManualScan | null>(null)
  const [seasons, setSeasons] = useState<ManualSeason[]>([])
  const [openSeason, setOpenSeason] = useState<number | null>(null)
  const [destId, setDestId] = useState<number | null>(defaultDestId)
  const [advanced, setAdvanced] = useState<ConvertOptions | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const navigate = useNavigate()
  useScrollLock()

  useEffect(() => {
    api<Language[]>('/api/languages').then(setLanguages).catch(() => {})
  }, [])

  const total = useMemo(
    () => seasons.reduce((n, s) => n + s.rows.filter((r) => r.include).length, 0),
    [seasons])

  async function search() {
    if (!query.trim() || searching) return
    setSearching(true)
    setError(null)
    try {
      const page = await api<MoviePage>(
        `/api/series?q=${encodeURIComponent(query)}`)
      setResults(page.results)
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setSearching(false)
    }
  }

  async function runScan() {
    if (!series || !origRoot || !dubRoot || busy) return
    setBusy(true)
    setError(null)
    try {
      const data = await post<ManualScan>('/api/series/manual/scan', {
        tmdb_id: series.id, original_root: origRoot, dubbed_root: dubRoot,
      })
      setScan(data)
      setSeasons(data.seasons)
      setOpenSeason(data.seasons[0]?.season ?? null)
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  /** Arquivos disponíveis de um lado, para os dropdowns daquela temporada. */
  function options(season: number, side: 'orig' | 'dub') {
    const lado = side === 'orig' ? scan?.original : scan?.dubbed
    return lado?.seasons?.[String(season)]?.files ?? []
  }

  function patch(season: number, episode: number, fields: Partial<ManualRow>) {
    setSeasons((prev) => prev.map((s) => s.season !== season ? s : {
      ...s,
      rows: s.rows.map((r) => r.episode !== episode ? r : { ...r, ...fields }),
    }))
  }

  /** Ações em massa: é isto que "troca a ordem" sem 24 cliques. */
  function repair(season: number, how: 'position' | 'shift+1' | 'shift-1') {
    setSeasons((prev) => prev.map((s) => {
      if (s.season !== season) return s
      const arquivos = options(season, 'dub').map((f) => f.path)
      const rows = s.rows.map((r, i) => {
        if (how === 'position') return { ...r, dubbed: arquivos[i] ?? null }
        const atual = arquivos.indexOf(r.dubbed ?? '')
        const alvo = atual + (how === 'shift+1' ? 1 : -1)
        return { ...r, dubbed: arquivos[alvo] ?? null }
      })
      return { ...s, rows: rows.map((r) => ({ ...r, include: !!(r.original && r.dubbed) })) }
    }))
  }

  async function submit() {
    if (!series || !total || busy) return
    setBusy(true)
    setError(null)
    try {
      const rows = seasons.flatMap((s) => s.rows.filter((r) => r.include))
      const job = await post<Job>('/api/jobs/series/manual', {
        tmdb_id: series.id, language, rows,
        destination_id: destId, convert: advanced,
      })
      navigate(`/jobs/${job.id}`)
    } catch (e) {
      setError((e as Error).message)
      setBusy(false)
    }
  }

  return (
    <div className="fixed inset-0 z-40 flex items-start justify-center overflow-auto bg-black/70 p-4">
      <div className="my-8 w-full max-w-4xl rounded-2xl border border-zinc-800 bg-zinc-900 p-5">
        <div className="mb-4 flex items-center gap-2">
          <Tv width={20} height={20} className="text-blue-400" />
          <h2 className="flex-1 text-lg font-semibold">Adicionar série (arquivos locais)</h2>
          <button onClick={onClose} className="text-zinc-500 hover:text-zinc-300">
            <Xmark width={20} height={20} />
          </button>
        </div>

        {/* 1. série */}
        <Step n={1} title="Série">
          {series ? (
            <div className="flex items-center gap-2 text-sm">
              <span className="font-medium">{series.title}</span>
              <span className="text-zinc-500">({series.year})</span>
              <button onClick={() => { setSeries(null); setScan(null); setSeasons([]) }}
                className="text-xs text-blue-400 hover:underline">trocar</button>
            </div>
          ) : (
            <>
              <div className="flex gap-2">
                <input
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                  onKeyDown={(e) => e.key === 'Enter' && void search()}
                  placeholder="Nome da série"
                  className="flex-1 rounded-lg border border-zinc-700 bg-zinc-800 px-3 py-1.5 text-sm"
                />
                <button onClick={() => void search()} disabled={searching}
                  className="inline-flex items-center gap-1.5 rounded-lg bg-zinc-700 px-3 py-1.5 text-sm hover:bg-zinc-600 disabled:opacity-50">
                  <Search width={15} height={15} /> Buscar
                </button>
              </div>
              {results && (
                <ul className="mt-2 max-h-48 divide-y divide-zinc-800 overflow-auto rounded-lg border border-zinc-800">
                  {results.slice(0, 12).map((s) => (
                    <li key={s.id}>
                      <button onClick={() => setSeries(s)}
                        className="w-full px-3 py-2 text-left text-sm hover:bg-zinc-800/60">
                        {s.title} <span className="text-zinc-500">({s.year})</span>
                      </button>
                    </li>
                  ))}
                  {!results.length && (
                    <li className="px-3 py-2 text-sm text-zinc-500">Nada encontrado.</li>
                  )}
                </ul>
              )}
            </>
          )}
        </Step>

        {/* 2. pastas */}
        {series && (
          <Step n={2} title="Pastas dos arquivos">
            <RootRow icon={MediaVideo} label="Original (vídeo)" value={origRoot}
              onPick={() => setPicking({ side: 'orig' })} />
            <RootRow icon={SoundHigh} label="Dublado (áudio)" value={dubRoot}
              onPick={() => setPicking({ side: 'dub' })} />
            <button onClick={() => void runScan()} disabled={!origRoot || !dubRoot || busy}
              className="mt-2 inline-flex items-center gap-1.5 rounded-lg bg-zinc-700 px-3 py-1.5 text-sm hover:bg-zinc-600 disabled:opacity-50">
              <Search width={15} height={15} /> {busy ? 'Lendo as pastas…' : 'Ler as pastas'}
            </button>
          </Step>
        )}

        {/* 3. temporadas */}
        {seasons.length > 0 && (
          <Step n={3} title="Temporadas encontradas">
            <div className="space-y-2">
              {seasons.map((s) => (
                <SeasonCard
                  key={s.season}
                  season={s}
                  open={openSeason === s.season}
                  onToggle={() => setOpenSeason(openSeason === s.season ? null : s.season)}
                  origOptions={options(s.season, 'orig')}
                  dubOptions={options(s.season, 'dub')}
                  onPatch={patch}
                  onRepair={repair}
                  onPickFile={(ep, side) => setPickFile({ season: s.season, ep, side })}
                />
              ))}
            </div>
          </Step>
        )}

        {/* 4. destino e conversão */}
        {seasons.length > 0 && (
          <Step n={4} title="Destino e conversão">
            <div className="flex flex-wrap gap-3">
              <label className="text-sm">
                <span className="mr-2 text-zinc-400">Idioma dublado</span>
                <select value={language} onChange={(e) => setLanguage(e.target.value)}
                  className="rounded-lg border border-zinc-700 bg-zinc-800 px-2 py-1 text-sm">
                  {languages.map((l) => (
                    <option key={l.code} value={l.code}>{l.label}</option>
                  ))}
                </select>
              </label>
              <label className="text-sm">
                <span className="mr-2 text-zinc-400">Destino</span>
                <select value={destId ?? ''}
                  onChange={(e) => setDestId(e.target.value ? Number(e.target.value) : null)}
                  className="rounded-lg border border-zinc-700 bg-zinc-800 px-2 py-1 text-sm">
                  <option value="">Padrão de séries</option>
                  {destinations.map((d) => (
                    <option key={d.id} value={d.id}>{d.label}</option>
                  ))}
                </select>
              </label>
            </div>
            <div className="mt-3">
              <AdvancedOptions value={advanced} onChange={setAdvanced} />
            </div>
          </Step>
        )}

        {error && <div className="mt-3 rounded-lg bg-red-950/60 px-3 py-2 text-sm text-red-300">{error}</div>}

        <div className="mt-5 flex items-center justify-end gap-2">
          <button onClick={onClose}
            className="rounded-lg border border-zinc-700 px-4 py-2 text-sm text-zinc-300 hover:bg-zinc-800">
            Cancelar
          </button>
          <button onClick={() => void submit()} disabled={!total || busy}
            className="inline-flex items-center gap-1.5 rounded-lg bg-blue-600 px-4 py-2 text-sm font-semibold hover:bg-blue-500 disabled:opacity-50">
            <Play width={15} height={15} />
            Converter {total ? `${total} episódio(s)` : ''}
          </button>
        </div>
      </div>

      {picking && (
        <FolderPicker
          mode="dir"
          title={picking.side === 'orig' ? 'Pasta do original (vídeo)' : 'Pasta do dublado (áudio)'}
          start={picking.side === 'orig' ? origRoot || null : dubRoot || null}
          onPick={(p) => {
            picking.side === 'orig' ? setOrigRoot(p) : setDubRoot(p)
            setPicking(null)
          }}
          onClose={() => setPicking(null)}
        />
      )}
      {pickFile && (
        <FolderPicker
          mode="file"
          title={`Arquivo ${pickFile.side === 'orig' ? 'original' : 'dublado'} de S${String(pickFile.season).padStart(2, '0')}E${String(pickFile.ep).padStart(2, '0')}`}
          start={pickFile.side === 'orig' ? origRoot || null : dubRoot || null}
          onPick={(p) => {
            patch(pickFile.season, pickFile.ep,
              pickFile.side === 'orig' ? { original: p, include: true } : { dubbed: p, include: true })
            setPickFile(null)
          }}
          onClose={() => setPickFile(null)}
        />
      )}
    </div>
  )
}

function Step({ n, title, children }: { n: number; title: string; children: React.ReactNode }) {
  return (
    <section className="mb-4 rounded-xl border border-zinc-800 p-3">
      <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-zinc-500">
        {n}. {title}
      </h3>
      {children}
    </section>
  )
}

function RootRow({ icon: Icon, label, value, onPick }: {
  icon: typeof MediaVideo
  label: string
  value: string
  onPick: () => void
}) {
  return (
    <div className="mb-2 flex items-center gap-2">
      <Icon width={15} height={15} className="shrink-0 text-zinc-500" />
      <span className="w-32 shrink-0 text-sm text-zinc-400">{label}</span>
      <code className="min-w-0 flex-1 truncate rounded-lg border border-zinc-800 bg-zinc-950/60 px-2 py-1 text-xs text-zinc-300">
        {value || 'nenhuma pasta escolhida'}
      </code>
      <button onClick={onPick}
        className="inline-flex shrink-0 items-center gap-1 rounded-lg border border-zinc-700 px-2 py-1 text-xs text-zinc-300 hover:bg-zinc-800">
        <Folder width={13} height={13} /> Escolher
      </button>
    </div>
  )
}

function SeasonCard({ season, open, onToggle, origOptions, dubOptions, onPatch, onRepair, onPickFile }: {
  season: ManualSeason
  open: boolean
  onToggle: () => void
  origOptions: { path: string; name: string }[]
  dubOptions: { path: string; name: string }[]
  onPatch: (season: number, episode: number, fields: Partial<ManualRow>) => void
  onRepair: (season: number, how: 'position' | 'shift+1' | 'shift-1') => void
  onPickFile: (episode: number, side: 'orig' | 'dub') => void
}) {
  const marcados = season.rows.filter((r) => r.include).length
  const faltando = season.rows.filter((r) => !r.original || !r.dubbed).length
  return (
    <div className="rounded-lg border border-zinc-800 bg-zinc-950/40">
      <button onClick={onToggle} className="w-full px-3 py-2 text-left hover:bg-zinc-800/40">
        <div className="flex items-center gap-2">
          <span className="text-sm font-semibold">Temporada {season.season}</span>
          <span className="text-xs text-zinc-500">
            {marcados} de {season.rows.length} episódio(s)
          </span>
          {faltando > 0 && (
            <span className="rounded bg-amber-950/70 px-1.5 py-0.5 text-xs text-amber-300">
              {faltando} sem par
            </span>
          )}
          <span className="ml-auto text-xs text-blue-400">
            {open ? 'fechar' : 'editar match manualmente'}
          </span>
        </div>
        <div className="mt-1 space-y-0.5 text-xs text-zinc-500">
          <SideLine icon={MediaVideo} label="Vídeo" side={season.original} />
          <SideLine icon={SoundHigh} label="Áudio" side={season.dubbed} />
        </div>
      </button>

      {open && (
        <div className="border-t border-zinc-800 p-2">
          <div className="mb-2 flex flex-wrap gap-1.5">
            <Bulk onClick={() => onRepair(season.season, 'position')}>Parear por posição</Bulk>
            <Bulk onClick={() => onRepair(season.season, 'shift-1')}>Deslocar dublado −1</Bulk>
            <Bulk onClick={() => onRepair(season.season, 'shift+1')}>Deslocar dublado +1</Bulk>
          </div>
          <table className="w-full text-xs">
            <thead className="text-zinc-500">
              <tr>
                <th className="w-8 py-1"></th>
                <th className="py-1 text-left">Episódio</th>
                <th className="py-1 text-left">Original (vídeo)</th>
                <th className="py-1 text-left">Dublado (áudio)</th>
              </tr>
            </thead>
            <tbody>
              {season.rows.map((r) => (
                <tr key={r.episode} className={r.include ? '' : 'opacity-40'}>
                  <td className="py-1">
                    <input type="checkbox" checked={r.include}
                      onChange={(e) => onPatch(season.season, r.episode, { include: e.target.checked })} />
                  </td>
                  <td className="py-1 pr-2 align-top">
                    <div className="font-medium text-zinc-300">
                      E{String(r.episode).padStart(2, '0')}
                    </div>
                    <div className="truncate text-zinc-600">{r.name}</div>
                  </td>
                  <td className="py-1 pr-2">
                    <FileSelect value={r.original} options={origOptions}
                      duration={r.orig_duration}
                      onChange={(v) => onPatch(season.season, r.episode, { original: v })}
                      onBrowse={() => onPickFile(r.episode, 'orig')} />
                  </td>
                  <td className="py-1">
                    <FileSelect value={r.dubbed} options={dubOptions}
                      duration={r.dub_duration}
                      onChange={(v) => onPatch(season.season, r.episode, { dubbed: v })}
                      onBrowse={() => onPickFile(r.episode, 'dub')} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

function SideLine({ icon: Icon, label, side }: {
  icon: typeof MediaVideo
  label: string
  side: ManualSeason['original']
}) {
  return (
    <div className="flex items-center gap-1.5">
      <Icon width={12} height={12} className="shrink-0" />
      <span className="w-12 shrink-0">{label}</span>
      <span className="shrink-0 rounded bg-zinc-800 px-1.5 text-zinc-400">{side.order}</span>
      <span className="min-w-0 flex-1 truncate">{side.dir || '—'}</span>
      {side.dirs > 1 && (
        <span className="shrink-0 rounded bg-amber-950/70 px-1 text-amber-300"
          title="A pasta escolhida mistura séries/versões: só a pasta acima entra no pareamento">
          {side.dirs} pastas
        </span>
      )}
      <span className="shrink-0">{side.files} arq · {side.episodes} ep</span>
    </div>
  )
}

function Bulk({ onClick, children }: { onClick: () => void; children: React.ReactNode }) {
  return (
    <button onClick={onClick}
      className="rounded border border-zinc-700 px-2 py-0.5 text-xs text-zinc-300 hover:bg-zinc-800">
      {children}
    </button>
  )
}

function FileSelect({ value, options, duration, onChange, onBrowse }: {
  value: string | null
  options: { path: string; name: string }[]
  duration: number | null
  onChange: (v: string | null) => void
  onBrowse: () => void
}) {
  // arquivo escolhido fora da pasta lida ainda precisa aparecer no dropdown
  const extra = value && !options.some((o) => o.path === value)
    ? [{ path: value, name: value.split('/').pop() ?? value }] : []
  return (
    <div className="flex items-center gap-1">
      <select
        value={value ?? ''}
        onChange={(e) => onChange(e.target.value || null)}
        className={`min-w-0 flex-1 rounded border bg-zinc-800 px-1 py-0.5 text-xs ${
          value ? 'border-zinc-700' : 'border-amber-800 text-amber-300'}`}
      >
        <option value="">— sem arquivo —</option>
        {[...options, ...extra].map((o) => (
          <option key={o.path} value={o.path}>{o.name}</option>
        ))}
      </select>
      <span className="w-12 shrink-0 text-right text-zinc-600">{fmtDur(duration)}</span>
      <button onClick={onBrowse} title="Escolher outro arquivo no disco"
        className="shrink-0 rounded border border-zinc-700 p-0.5 text-zinc-400 hover:bg-zinc-800">
        <Folder width={12} height={12} />
      </button>
    </div>
  )
}
