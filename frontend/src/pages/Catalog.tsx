import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { Folder, MediaVideoList, NavArrowRight, Plus, Search, Tv, WarningTriangle, Xmark } from 'iconoir-react'
import { api, type CatalogItem, type CatalogList, type Destination } from '../api'
import AddMovieModal from '../components/AddMovieModal'
import AddSeriesModal from '../components/AddSeriesModal'
import { InfiniteScrollFooter, useInfiniteScroll } from '../components/InfiniteScroll'
import { DiskBar, Empty } from '../components/ui'

// critérios de ordenação disponíveis no catálogo
type SortKey = 'title' | 'year' | 'size'
type SortDir = 'asc' | 'desc'

const SORTS: { key: SortKey; label: string }[] = [
  { key: 'title', label: 'Título' },
  { key: 'year', label: 'Ano' },
  { key: 'size', label: 'Tamanho' },
]

// linhas por página do scroll infinito (a varredura de cada pasta é o caro)
const PER_PAGE = 50

export default function Catalog() {
  const [destinations, setDestinations] = useState<Destination[]>([])
  // catálogo de FILMES e de SÉRIES são separados (destinos por mídia)
  const [media, setMedia] = useState<'movie' | 'tv'>('movie')
  const [destId, setDestId] = useState<number | null>(null)
  const [query, setQuery] = useState('')
  // a busca roda no backend (acha itens de páginas ainda não carregadas):
  // espera a digitação parar para não pedir uma listagem por tecla
  const [q, setQ] = useState('')
  const [sortKey, setSortKey] = useState<SortKey>('title')
  const [sortDir, setSortDir] = useState<SortDir>('asc')
  const [adding, setAdding] = useState<'movie' | 'tv' | null>(null)

  useEffect(() => {
    api<Destination[]>('/api/destinations')
      .then((ds) => {
        setDestinations(ds)
        const mine = ds.filter((d) => (d.media ?? 'movie') === 'movie')
        setDestId(mine.find((d) => d.is_default)?.id ?? mine[0]?.id ?? null)
      })
      .catch(() => {})
  }, [])

  // destinos da mídia ativa; trocar o toggle pula para o padrão daquela mídia
  const mediaDests = destinations.filter((d) => (d.media ?? 'movie') === media)

  function switchMedia(m: 'movie' | 'tv') {
    setMedia(m)
    setQuery('')
    setQ('')  // já, sem esperar o debounce: senão pediria duas listagens
    const mine = destinations.filter((d) => (d.media ?? 'movie') === m)
    // sem destino da mídia: destId null desliga a listagem (e a esvazia)
    setDestId(mine.find((d) => d.is_default)?.id ?? mine[0]?.id ?? null)
  }

  useEffect(() => {
    const t = setTimeout(() => setQ(query.trim()), 250)
    return () => clearTimeout(t)
  }, [query])

  // scroll infinito: filtro, ordenação e página vão para o backend. Mantém
  // as linhas da listagem anterior até a nova chegar (busca enquanto digita
  // sem piscar "Carregando...")
  const scroll = useInfiniteScroll<CatalogItem, CatalogList>({
    key: `${destId}|${q}|${sortKey}|${sortDir}`,
    enabled: destId != null,
    keepPrevious: true,
    fetchPage: (page, signal) => api<CatalogList>(
      `/api/catalog?destination_id=${destId}&q=${encodeURIComponent(q)}`
      + `&sort=${sortKey}&order=${sortDir}&page=${page}&per_page=${PER_PAGE}`, { signal }),
    itemKey: (it) => it.folder,
  })
  // `data` e `items` vêm sempre da mesma resposta (até nas linhas antigas
  // mantidas durante a troca), então o link usa o destino DELA
  const data = scroll.last
  const items = scroll.items ?? []
  // a lista na tela é a da busca/ordem atual (não a anterior aguardando troca)
  const ready = scroll.items !== null && !scroll.refreshing && !scroll.error

  return (
    <div>
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="flex-1 text-lg font-semibold">Catálogo</h1>
        {/* bibliotecas separadas: o toggle troca o conjunto de destinos */}
        <div className="flex gap-1 rounded-lg border border-zinc-700 bg-zinc-900 p-1">
          {([['movie', 'Filmes'], ['tv', 'Séries']] as const).map(([k, label]) => (
            <button
              key={k}
              onClick={() => switchMedia(k)}
              className={`rounded-md px-3 py-0.5 text-sm transition-colors ${
                media === k
                  ? 'bg-blue-600 font-semibold text-white'
                  : 'text-zinc-400 hover:text-zinc-200'
              }`}
            >
              {label}
            </button>
          ))}
        </div>
        {/* segue a aba: na biblioteca de séries o botão é o de série */}
        <button
          onClick={() => setAdding(media)}
          title={media === 'tv'
            ? 'Merge manual de série: pastas de original e dublado que já estão no disco'
            : 'Conversão manual: merge de dois arquivos que já estão no disco'}
          className="flex items-center gap-1.5 rounded-lg bg-blue-600 px-3 py-1.5 text-sm font-semibold hover:bg-blue-500"
        >
          <Plus width={16} height={16} /> Adicionar {media === 'tv' ? 'série' : 'filme'}
        </button>
        <label className="flex items-center gap-2 text-sm">
          Destino:
          <select
            value={destId ?? ''}
            onChange={(e) => setDestId(Number(e.target.value))}
            className="max-w-56 rounded-lg border border-zinc-700 bg-zinc-800 px-2 py-1.5 text-sm"
          >
            {mediaDests.map((d) => (
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

      {data?.exists && data.total_all > 0 && (
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
          <span className="text-sm whitespace-nowrap text-zinc-500" title="Pastas neste destino">
            {data.total === data.total_all
              ? `${data.total_all} ${data.total_all === 1 ? 'item' : 'itens'}`
              : `${data.total} de ${data.total_all} itens`}
          </span>
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

      {scroll.items === null && scroll.loading && <Empty>Carregando...</Empty>}
      {data && !data.exists && (
        <div className="mt-4 flex items-center gap-2 rounded-xl border border-amber-900/60 bg-amber-950/20 p-4 text-sm text-amber-300">
          <WarningTriangle width={18} height={18} />
          A pasta deste destino não existe (ou não está montada) nesta máquina.
        </div>
      )}
      {mediaDests.length === 0 && (
        <Empty>
          Nenhum destino de {media === 'tv' ? 'séries' : 'filmes'} cadastrado —
          crie um em Configurações → Destinos.
        </Empty>
      )}
      {ready && data?.exists && data.total_all === 0 && (
        <Empty>{media === 'tv' ? 'Nenhuma série' : 'Nenhum filme'} neste destino.</Empty>
      )}
      {ready && data?.exists && data.total_all > 0 && data.total === 0 && (
        <Empty>{media === 'tv' ? 'Nenhuma série' : 'Nenhum filme'} corresponde à busca.</Empty>
      )}

      <div className={`mt-5 flex flex-col gap-2 transition-opacity ${scroll.refreshing ? 'opacity-60' : ''}`}>
        {items.map((it) => (
          <Link
            key={it.folder}
            to={`/catalog/item?destination_id=${data?.destination.id ?? destId}&folder=${encodeURIComponent(it.folder)}`}
            className="flex items-center gap-3 rounded-xl bg-zinc-900 px-4 py-3 hover:bg-zinc-800/70"
          >
            {it.type === 'series' ? (
              <Tv width={20} height={20} className="shrink-0 text-sky-400" />
            ) : it.has_video ? (
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
      {/* sentinela do scroll infinito + carregando / fim / tentar de novo */}
      <InfiniteScrollFooter scroll={scroll} />

      {adding === 'tv' && (
        <AddSeriesModal
          destinations={destinations.filter((d) => d.media === 'tv')}
          defaultDestId={destId}
          onClose={() => setAdding(null)}
        />
      )}
      {adding === 'movie' && (
        <AddMovieModal
          destinations={destinations.filter((d) => (d.media ?? 'movie') === 'movie')}
          defaultDestId={destId}
          onClose={() => setAdding(null)}
        />
      )}
    </div>
  )
}
