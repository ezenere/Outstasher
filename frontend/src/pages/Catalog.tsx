import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { Folder, MediaVideoList, NavArrowRight, Plus, Search, WarningTriangle, Xmark } from 'iconoir-react'
import { api, type CatalogItem, type CatalogList, type Destination } from '../api'
import AddMovieModal from '../components/AddMovieModal'
import { DiskBar, Empty } from '../components/ui'

// critérios de ordenação disponíveis no catálogo
type SortKey = 'title' | 'year' | 'size'
type SortDir = 'asc' | 'desc'

const SORTS: { key: SortKey; label: string }[] = [
  { key: 'title', label: 'Título' },
  { key: 'year', label: 'Ano' },
  { key: 'size', label: 'Tamanho' },
]

export default function Catalog() {
  const [destinations, setDestinations] = useState<Destination[]>([])
  const [destId, setDestId] = useState<number | null>(null)
  const [data, setData] = useState<CatalogList | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [query, setQuery] = useState('')
  const [sortKey, setSortKey] = useState<SortKey>('title')
  const [sortDir, setSortDir] = useState<SortDir>('asc')
  const [adding, setAdding] = useState(false)

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

  // filtra pelo texto (título ou pasta) e ordena pelo critério escolhido
  const items = useMemo(() => {
    const q = query.trim().toLowerCase()
    const filtered = (data?.items ?? []).filter(
      (it) => !q || it.title.toLowerCase().includes(q) || it.folder.toLowerCase().includes(q),
    )
    const dir = sortDir === 'asc' ? 1 : -1
    const cmp = (a: CatalogItem, b: CatalogItem) => {
      if (sortKey === 'title') return a.title.localeCompare(b.title, 'pt-BR') * dir
      if (sortKey === 'size') return (a.size - b.size) * dir
      // ano: itens sem ano vão sempre para o fim, independente da direção
      const ay = a.year ? Number(a.year) : null
      const by = b.year ? Number(b.year) : null
      if (ay == null && by == null) return a.title.localeCompare(b.title, 'pt-BR')
      if (ay == null) return 1
      if (by == null) return -1
      return (ay - by) * dir || a.title.localeCompare(b.title, 'pt-BR')
    }
    return [...filtered].sort(cmp)
  }, [data, query, sortKey, sortDir])

  return (
    <div>
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="flex-1 text-lg font-semibold">Catálogo</h1>
        <button
          onClick={() => setAdding(true)}
          title="Conversão manual: merge de dois arquivos que já estão no disco"
          className="flex items-center gap-1.5 rounded-lg bg-blue-600 px-3 py-1.5 text-sm font-semibold hover:bg-blue-500"
        >
          <Plus width={16} height={16} /> Adicionar filme
        </button>
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
        <div className="mt-1">
          <p className="font-mono text-xs text-zinc-500">{data.destination.path}</p>
          <div className="mt-2 max-w-md"><DiskBar disk={data.destination.disk} /></div>
        </div>
      )}

      {data?.exists && data.items.length > 0 && (
        <div className="mt-4 flex flex-col gap-3 sm:flex-row sm:items-center">
          <div className="relative flex-1">
            <Search width={15} height={15} className="absolute top-1/2 left-3 -translate-y-1/2 text-zinc-500" />
            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Buscar por título ou pasta..."
              className="w-full rounded-lg border border-zinc-700 bg-zinc-900 py-2 pr-8 pl-9 text-sm outline-none focus:border-blue-500"
            />
            {query && (
              <button
                onClick={() => setQuery('')}
                className="absolute top-1/2 right-2 -translate-y-1/2 text-zinc-500 hover:text-zinc-300"
                title="Limpar"
              >
                <Xmark width={15} height={15} />
              </button>
            )}
          </div>
          <div className="flex items-center gap-1.5">
            <span className="text-sm text-zinc-500">Ordenar:</span>
            {SORTS.map((s) => {
              const active = sortKey === s.key
              return (
                <button
                  key={s.key}
                  onClick={() => {
                    if (active) setSortDir((d) => (d === 'asc' ? 'desc' : 'asc'))
                    else {
                      setSortKey(s.key)
                      setSortDir('asc')
                    }
                  }}
                  title={active ? (sortDir === 'asc' ? 'crescente — clique para inverter' : 'decrescente — clique para inverter') : `Ordenar por ${s.label.toLowerCase()}`}
                  className={`rounded-lg px-2.5 py-1.5 text-sm transition-colors ${
                    active ? 'bg-blue-600 font-semibold text-white' : 'bg-zinc-800 text-zinc-300 hover:bg-zinc-700'
                  }`}
                >
                  {s.label}
                  {active && <span className="ml-1">{sortDir === 'asc' ? '↑' : '↓'}</span>}
                </button>
              )
            })}
          </div>
        </div>
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
      {data?.exists && data.items.length > 0 && items.length === 0 && (
        <Empty>Nenhum filme corresponde à busca.</Empty>
      )}

      <div className="mt-5 flex flex-col gap-2">
        {items.map((it) => (
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

      {adding && (
        <AddMovieModal destinations={destinations} defaultDestId={destId} onClose={() => setAdding(false)} />
      )}
    </div>
  )
}
