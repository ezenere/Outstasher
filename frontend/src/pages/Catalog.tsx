import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { Folder, MediaVideoList, NavArrowRight, WarningTriangle } from 'iconoir-react'
import { api, type CatalogList, type Destination } from '../api'
import { Empty } from '../components/ui'

export default function Catalog() {
  const [destinations, setDestinations] = useState<Destination[]>([])
  const [destId, setDestId] = useState<number | null>(null)
  const [data, setData] = useState<CatalogList | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    api<Destination[]>('/api/destinations')
      .then((ds) => {
        setDestinations(ds)
        setDestId(ds.find((d) => d.is_default)?.id ?? ds[0]?.id ?? null)
      })
      .catch(() => {})
  }, [])

  useEffect(() => {
    if (destId == null) return
    setLoading(true)
    setError(null)
    api<CatalogList>(`/api/catalog?destination_id=${destId}`)
      .then(setData)
      .catch((e) => setError((e as Error).message))
      .finally(() => setLoading(false))
  }, [destId])

  return (
    <div>
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="flex-1 text-lg font-semibold">Catálogo</h1>
        <label className="flex items-center gap-2 text-sm">
          Destino:
          <select
            value={destId ?? ''}
            onChange={(e) => setDestId(Number(e.target.value))}
            className="max-w-56 rounded-lg border border-zinc-700 bg-zinc-800 px-2 py-1.5 text-sm"
          >
            {destinations.map((d) => (
              <option key={d.id} value={d.id}>
                {d.label}{d.is_default ? ' ★' : ''}
              </option>
            ))}
          </select>
        </label>
      </div>
      {data?.destination && (
        <p className="mt-1 font-mono text-xs text-zinc-500">{data.destination.path}</p>
      )}

      {loading && <Empty>Carregando...</Empty>}
      {error && <Empty>Erro: {error}</Empty>}
      {data && !data.exists && (
        <div className="mt-4 flex items-center gap-2 rounded-xl border border-amber-900/60 bg-amber-950/20 p-4 text-sm text-amber-300">
          <WarningTriangle width={18} height={18} />
          A pasta deste destino não existe (ou não está montada) nesta máquina.
        </div>
      )}
      {data?.exists && data.items.length === 0 && <Empty>Nenhum filme neste destino.</Empty>}

      <div className="mt-5 flex flex-col gap-2">
        {data?.items.map((it) => (
          <Link
            key={it.folder}
            to={`/catalog/item?destination_id=${destId}&folder=${encodeURIComponent(it.folder)}`}
            className="flex items-center gap-3 rounded-xl bg-zinc-900 px-4 py-3 hover:bg-zinc-800/70"
          >
            {it.has_video ? (
              <MediaVideoList width={20} height={20} className="shrink-0 text-blue-400" />
            ) : (
              <Folder width={20} height={20} className="shrink-0 text-zinc-500" />
            )}
            <div className="min-w-0 flex-1">
              <div className="truncate font-semibold">
                {it.title}
                {it.year && <span className="ml-1 font-normal text-zinc-400">({it.year})</span>}
              </div>
              <div className="truncate text-xs text-zinc-500">{it.folder}</div>
            </div>
            <div className="text-right text-xs text-zinc-400">
              <div>{it.size_human}</div>
              <div>{it.file_count} arquivo{it.file_count === 1 ? '' : 's'}</div>
            </div>
            <NavArrowRight width={18} height={18} className="shrink-0 text-zinc-600" />
          </Link>
        ))}
      </div>
    </div>
  )
}
