import { useCallback, useEffect, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import {
  ClosedCaptionsTag, Compress, EditPencil, Label, MediaVideo, MusicNote, NavArrowDown,
  NavArrowLeft, NavArrowRight, Page, Trash, WarningTriangle,
} from 'iconoir-react'
import { api, del, post, type CatalogDetail, type CatalogFile, type Stream } from '../api'
import { Empty } from '../components/ui'
import { useDialog } from '../components/Dialog'
import RecompressModal from '../components/RecompressModal'

export default function CatalogItem() {
  const dialog = useDialog()
  const [params] = useSearchParams()
  const destId = params.get('destination_id')
  const folder = params.get('folder') ?? ''
  const [detail, setDetail] = useState<CatalogDetail | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [open, setOpen] = useState<Set<string>>(new Set())
  const [recompress, setRecompress] = useState<CatalogFile | null>(null)
  const navigate = useNavigate()

  const qs = `destination_id=${destId}&folder=${encodeURIComponent(folder)}`
  // o [tmdbid-N] na pasta é o que o Jellyfin usa para identificar o filme
  const taggedId = /\[tmdbid-(\d+)\]/i.exec(folder)?.[1]

  const reload = useCallback(async () => {
    try {
      setDetail(await api<CatalogDetail>(`/api/catalog/item?${qs}`))
      setError(null)
    } catch (e) {
      setError((e as Error).message)
    }
  }, [qs])

  useEffect(() => {
    void reload()
  }, [reload])

  function toggle(rel: string) {
    setOpen((prev) => {
      const next = new Set(prev)
      next.has(rel) ? next.delete(rel) : next.add(rel)
      return next
    })
  }

  async function removeFile(f: CatalogFile) {
    if (!(await dialog.confirm({
      title: 'Remover arquivo',
      message: `Remover o arquivo "${f.name}"?`,
      confirmText: 'Remover', tone: 'danger',
    }))) return
    try {
      await del(`/api/catalog/file?${qs}&rel=${encodeURIComponent(f.rel)}`)
      void reload()
    } catch (e) {
      await dialog.alert({ title: 'Erro', message: (e as Error).message })
    }
  }

  async function renameFile(f: CatalogFile) {
    const novo = await dialog.prompt({
      title: 'Renomear arquivo',
      message: 'Sem digitar uma extensão, a original é mantida.',
      defaultValue: f.name, confirmText: 'Renomear',
    })
    if (novo == null) return // cancelou
    if (novo.trim() === '' || novo.trim() === f.name) return // vazio ou sem mudança
    try {
      await post('/api/catalog/file/rename', {
        folder, destination_id: destId ? Number(destId) : null, rel: f.rel, new_name: novo.trim(),
      })
      void reload()
    } catch (e) {
      await dialog.alert({ title: 'Erro', message: (e as Error).message })
    }
  }

  async function tagTmdbId() {
    if (!detail?.tmdb) return
    const novo = `${detail.title}${detail.year ? ` (${detail.year})` : ''} [tmdbid-${detail.tmdb.id}]`
    if (!(await dialog.confirm({
      title: 'Marcar com o ID do TMDB',
      message: `Renomear a pasta para "${novo}"? O Jellyfin usa esse ID para identificar o filme sem depender do título.`,
      confirmText: 'Renomear',
    }))) return
    try {
      const r = await post<{ folder: string }>('/api/catalog/item/tmdbid', {
        folder, destination_id: destId ? Number(destId) : null, tmdb_id: detail.tmdb.id,
      })
      // a pasta mudou de nome: a URL atual aponta para um caminho que não existe mais
      navigate(`/catalog/item?destination_id=${destId}&folder=${encodeURIComponent(r.folder)}`,
        { replace: true })
    } catch (e) {
      await dialog.alert({ title: 'Erro', message: (e as Error).message })
    }
  }

  async function removeFolder() {
    if (!detail) return
    if (!(await dialog.confirm({
      title: 'Remover pasta do filme',
      message: `Remover a pasta inteira do filme "${detail.title}"? Isso apaga todos os arquivos dentro dela.`,
      confirmText: 'Remover tudo', tone: 'danger',
    }))) return
    try {
      await del(`/api/catalog/item?${qs}`)
      navigate('/catalog')
    } catch (e) {
      await dialog.alert({ title: 'Erro', message: (e as Error).message })
    }
  }

  if (error) return <Empty>Erro: {error}</Empty>
  if (!detail) return <Empty>Carregando...</Empty>

  const m = detail.tmdb

  return (
    <div>
      <div className="flex items-center gap-3">
        <Link to="/catalog" className="rounded-lg border border-zinc-700 p-1.5 text-zinc-400 hover:text-zinc-200">
          <NavArrowLeft width={16} height={16} />
        </Link>
        <h1 className="flex-1 text-lg font-semibold">
          {detail.title}
          {detail.year && <span className="ml-1 font-normal text-zinc-400">({detail.year})</span>}
        </h1>
        {m && !taggedId && (
          <button
            onClick={tagTmdbId}
            title={`Renomear a pasta para incluir [tmdbid-${m.id}]`}
            className="flex items-center gap-1.5 rounded-lg border border-zinc-700 px-3 py-1.5 text-sm text-zinc-300 hover:border-blue-700 hover:text-blue-300"
          >
            <Label width={15} height={15} /> Marcar ID do TMDB
          </button>
        )}
        <button
          onClick={removeFolder}
          className="flex items-center gap-1.5 rounded-lg border border-red-900/60 px-3 py-1.5 text-sm text-red-400 hover:bg-red-950/40"
        >
          <Trash width={15} height={15} /> Remover pasta
        </button>
      </div>

      {/* cabeçalho com match TMDB */}
      <div className="mt-4 flex gap-4 rounded-xl bg-zinc-900 p-4">
        {m?.poster ? (
          <img src={m.poster} className="h-40 w-28 shrink-0 rounded-lg object-cover" />
        ) : (
          <div className="flex h-40 w-28 shrink-0 items-center justify-center rounded-lg bg-zinc-800 text-xs text-zinc-500">
            sem pôster
          </div>
        )}
        <div className="min-w-0 flex-1 text-sm">
          {m ? (
            <>
              <div className="font-semibold">
                {m.title}
                {m.original_title && m.original_title !== m.title && (
                  <span className="ml-1 font-normal text-zinc-400">/ {m.original_title}</span>
                )}
              </div>
              <div className="mt-0.5 text-zinc-400">
                {m.year}{m.rating ? ` · ⭐ ${m.rating.toFixed(1)}` : ''}
              </div>
              {m.overview && <p className="mt-2 line-clamp-4 text-zinc-300">{m.overview}</p>}
            </>
          ) : (
            <div className="flex items-center gap-2 text-zinc-400">
              <WarningTriangle width={16} height={16} /> Sem correspondência no TMDB.
            </div>
          )}
          <div className="mt-3 text-xs text-zinc-500">
            {detail.files.length} arquivo{detail.files.length === 1 ? '' : 's'} · {detail.size_human}
            <span className="ml-2 font-mono">{detail.folder}</span>
          </div>
        </div>
      </div>

      {/* arquivos com dropdown */}
      <h2 className="mt-6 mb-2 text-sm font-semibold text-zinc-400">Arquivos</h2>
      <div className="flex flex-col gap-2">
        {detail.files.map((f) => (
          <FileRow
            key={f.rel}
            file={f}
            open={open.has(f.rel)}
            onToggle={() => toggle(f.rel)}
            onRename={() => renameFile(f)}
            onRemove={() => removeFile(f)}
            onRecompress={f.category === 'video' ? () => setRecompress(f) : undefined}
          />
        ))}
      </div>

      {recompress && (
        <RecompressModal
          folder={folder}
          destinationId={destId ? Number(destId) : null}
          file={recompress}
          tmdb={detail.tmdb}
          onClose={() => setRecompress(null)}
        />
      )}
    </div>
  )
}

function FileRow({ file, open, onToggle, onRename, onRemove, onRecompress }: {
  file: CatalogFile
  open: boolean
  onToggle: () => void
  onRename: () => void
  onRemove: () => void
  onRecompress?: () => void
}) {
  const Icon = file.category === 'video' ? MediaVideo : file.category === 'subtitle' ? ClosedCaptionsTag : file.category === 'media' ? MusicNote : Page
  return (
    <div className="overflow-hidden rounded-xl bg-zinc-900">
      <div className="flex items-center gap-3 px-4 py-3">
        <button onClick={onToggle} className="text-zinc-500 hover:text-zinc-300">
          {open ? <NavArrowDown width={18} height={18} /> : <NavArrowRight width={18} height={18} />}
        </button>
        <Icon width={18} height={18} className="shrink-0 text-zinc-400" />
        <button onClick={onToggle} className="min-w-0 flex-1 text-left">
          <div className="truncate font-mono text-sm">{file.name}</div>
          <div className="text-xs text-zinc-500">
            {file.size_human}
            {file.duration ? ` · ${file.duration}` : ''}
            {file.counts ? ` · ${file.counts.video}V/${file.counts.audio}A/${file.counts.subtitle}S` : ''}
            {file.probe_error ? ' · (não sondável)' : ''}
          </div>
        </button>
        {onRecompress && (
          <button
            onClick={onRecompress}
            title="Recomprimir (converter com as opções avançadas)"
            className="rounded-lg border border-zinc-700 p-1.5 text-zinc-400 hover:border-purple-700 hover:text-purple-300"
          >
            <Compress width={14} height={14} />
          </button>
        )}
        <button
          onClick={onRename}
          title="Renomear arquivo"
          className="rounded-lg border border-zinc-700 p-1.5 text-zinc-400 hover:border-blue-700 hover:text-blue-300"
        >
          <EditPencil width={14} height={14} />
        </button>
        <button
          onClick={onRemove}
          title="Remover arquivo"
          className="rounded-lg border border-zinc-700 p-1.5 text-zinc-400 hover:border-red-900/60 hover:text-red-400"
        >
          <Trash width={14} height={14} />
        </button>
      </div>

      {open && (
        <div className="border-t border-zinc-800 px-4 py-3 text-sm">
          {file.probe_error && (
            <div className="mb-2 text-xs text-amber-400">ffprobe: {file.probe_error}</div>
          )}
          {(file.container || file.overall_bitrate) && (
            <div className="mb-3 flex flex-wrap gap-x-6 gap-y-1 text-xs text-zinc-400">
              {file.container && <span>Container: <span className="text-zinc-200">{file.container}</span></span>}
              {file.overall_bitrate && <span>Bitrate total: <span className="text-zinc-200">{file.overall_bitrate}</span></span>}
              {file.chapters ? <span>Capítulos: <span className="text-zinc-200">{file.chapters}</span></span> : null}
            </div>
          )}
          {file.streams?.length ? (
            <div className="flex flex-col gap-2">
              {file.streams.map((s) => <StreamCard key={s.index} s={s} />)}
            </div>
          ) : (
            !file.probe_error && <div className="text-xs text-zinc-500">Sem tracks (arquivo não-mídia).</div>
          )}
        </div>
      )}
    </div>
  )
}

const TYPE_STYLE: Record<string, string> = {
  video: 'bg-blue-950 text-blue-300',
  audio: 'bg-emerald-950 text-emerald-300',
  subtitle: 'bg-purple-950 text-purple-300',
}

function StreamCard({ s }: { s: Stream }) {
  const facts: [string, React.ReactNode][] = []
  const add = (k: string, v: unknown) => {
    if (v !== null && v !== undefined && v !== '' && v !== false) facts.push([k, String(v)])
  }

  if (s.type === 'video') {
    add('Resolução', s.resolution)
    add('FPS', s.fps)
    add('Bitrate', s.bitrate)
    add('Formato de pixel', s.pix_fmt)
    add('Profundidade', s.bit_depth ? `${s.bit_depth}-bit` : null)
    if (s.hdr) facts.push(['Faixa dinâmica', <span className="font-semibold text-amber-300">HDR</span>])
    add('Espaço de cor', s.color_space)
    add('Transfer', s.color_transfer)
    add('Primárias', s.color_primaries)
    add('Aspecto', s.aspect_ratio)
    add('Perfil', s.profile)
    add('Nível', s.level)
  } else if (s.type === 'audio') {
    add('Canais', s.channel_layout ?? s.channels)
    add('Sample rate', s.sample_rate)
    add('Bitrate', s.bitrate)
    add('Formato de amostra', s.sample_fmt)
    add('Perfil', s.profile)
  } else if (s.type === 'subtitle') {
    if (s.hearing_impaired) facts.push(['Acessibilidade', 'SDH'])
    add('Perfil', s.profile)
  }
  // extras crus do ffprobe
  Object.entries(s.raw ?? {}).forEach(([k, v]) => add(k, v))

  return (
    <div className="rounded-lg border border-zinc-800 bg-zinc-950/40 p-3">
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <span className={`rounded px-1.5 py-0.5 text-xs font-semibold uppercase ${TYPE_STYLE[s.type] ?? 'bg-zinc-800 text-zinc-300'}`}>
          {s.type}
        </span>
        <span className="text-xs text-zinc-500">#{s.index}</span>
        <span className="font-mono text-sm">{s.codec}</span>
        {s.language && <span className="rounded bg-zinc-800 px-1.5 py-0.5 text-xs">{s.language}</span>}
        {s.default && <span className="rounded bg-blue-950 px-1.5 py-0.5 text-xs text-blue-300">default</span>}
        {s.forced && <span className="rounded bg-amber-950 px-1.5 py-0.5 text-xs text-amber-300">forced</span>}
        {s.title && <span className="truncate text-xs text-zinc-400">“{s.title}”</span>}
      </div>
      {s.codec_long && s.codec_long !== s.codec && (
        <div className="mb-2 text-xs text-zinc-500">{s.codec_long}</div>
      )}
      <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs sm:grid-cols-3">
        {facts.map(([k, v], i) => (
          <div key={i} className="flex justify-between gap-2 border-b border-zinc-800/60 pb-0.5">
            <dt className="text-zinc-500">{k}</dt>
            <dd className="truncate text-right text-zinc-200" title={typeof v === 'string' ? v : undefined}>{v}</dd>
          </div>
        ))}
      </dl>
    </div>
  )
}
