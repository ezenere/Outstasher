import { useCallback, useEffect, useRef, useState } from 'react'

/** Uma página de uma listagem paginada (o que o `fetchPage` devolve). A
 *  resposta pode trazer mais campos (total, destino...): ficam em `last`. */
export interface InfinitePage<T> {
  items: T[]
  page: number
  pages: number
}

export interface InfiniteScroll<T, R extends InfinitePage<T>> {
  /** Itens acumulados; null = nada carregado ainda (1ª página a caminho). */
  items: T[] | null
  /** Última resposta recebida — os metadados da listagem (total etc.). */
  last: R | null
  loading: boolean
  /** Carregando a 1ª página de uma listagem nova (busca/filtro trocou). */
  refreshing: boolean
  error: string | null
  /** Chegou na última página. */
  done: boolean
  retry: () => void
  /** Ref do sentinela no fim da lista (o InfiniteScrollFooter já põe um). */
  sentinelRef: (el: Element | null) => void
}

/** Scroll infinito: carrega a página seguinte quando o sentinela no fim da
 *  lista chega perto da tela.
 *
 *  `key` identifica a listagem (busca, filtros, ordem): mudou, a lista
 *  recomeça da página 1 e a resposta atrasada da listagem anterior é
 *  descartada (a requisição é abortada e, se mesmo assim chegar, ignorada) —
 *  sem isso, digitar rápido ou trocar o toggle no meio de uma carga anexava
 *  resultados da busca velha. `fetchPage` pode mudar a cada render: vale o
 *  da render em que a página foi pedida, então tudo o que ele usa tem de
 *  estar na `key`.
 *
 *  Uma carga por vez; erro para o scroll até `retry` (sem martelar a API).
 *  `keepPrevious` mantém os itens da listagem anterior na tela até a nova
 *  chegar (busca enquanto digita, sem piscar "Carregando...").
 */
export function useInfiniteScroll<T, R extends InfinitePage<T> = InfinitePage<T>>({
  key, fetchPage, itemKey, enabled = true, keepPrevious = false, rootMargin = '0px 0px 800px 0px',
}: {
  key: string
  fetchPage: (page: number, signal: AbortSignal) => Promise<R>
  /** Identidade do item: dedup entre páginas (o TMDB às vezes repete). */
  itemKey: (item: T) => string | number
  enabled?: boolean
  keepPrevious?: boolean
  /** Folga abaixo da tela para pré-carregar antes de chegar ao fim. */
  rootMargin?: string
}): InfiniteScroll<T, R> {
  const [items, setItems] = useState<T[] | null>(null)
  const [last, setLast] = useState<R | null>(null)
  const [loading, setLoading] = useState(false)
  const [refreshing, setRefreshing] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [done, setDone] = useState(false)
  const [sentinel, setSentinel] = useState<Element | null>(null)

  const fetchRef = useRef(fetchPage)
  fetchRef.current = fetchPage
  const itemKeyRef = useRef(itemKey)
  itemKeyRef.current = itemKey
  // um controlador por listagem: trocar a key aborta o da anterior, e uma
  // resposta só é aplicada se o controlador dela ainda for o atual
  const ctl = useRef<AbortController | null>(null)
  const busy = useRef(false)
  const nextPage = useRef(1)

  const load = useCallback(async (page: number) => {
    const c = ctl.current
    if (!c || c.signal.aborted || busy.current) return
    busy.current = true
    setLoading(true)
    setError(null)
    try {
      const r = await fetchRef.current(page, c.signal)
      if (c.signal.aborted) return
      const ik = itemKeyRef.current
      setItems((cur) => {
        const base = page === 1 ? [] : (cur ?? [])
        const seen = new Set(base.map(ik))
        const fresh = r.items.filter((it) => {
          const k = ik(it)
          if (seen.has(k)) return false
          seen.add(k)
          return true
        })
        return [...base, ...fresh]
      })
      setLast(r)
      nextPage.current = r.page + 1
      // fim: última página, ou o servidor não avançou (pedido além do limite
      // dele devolve a última de novo — sem isto o sentinela pediria em laço)
      setDone(r.page >= r.pages || r.page < page)
    } catch (e) {
      if (c.signal.aborted) return
      // a 1ª página falhou: as linhas mantidas (keepPrevious) são de outra
      // busca e não podem ficar parecendo o resultado desta
      if (page === 1) setItems(null)
      setError((e as Error).message)
    } finally {
      if (ctl.current === c) {
        busy.current = false
        setLoading(false)
        setRefreshing(false)
      }
    }
  }, [])

  // listagem nova (key/enabled): zera e pede a página 1
  useEffect(() => {
    const c = new AbortController()
    ctl.current = c
    busy.current = false
    nextPage.current = 1
    setError(null)
    setDone(false)
    if (!enabled) {
      setItems(null)
      setLast(null)
      setLoading(false)
      setRefreshing(false)
    } else {
      if (!keepPrevious) setItems(null)
      setRefreshing(true)
      void load(1)
    }
    return () => c.abort()
  }, [key, enabled, keepPrevious, load])

  // o observer é recriado a cada carga concluída: observe() sempre entrega o
  // estado inicial, então se a página nova não encheu a tela (monitor alto,
  // busca com poucos itens por página) o sentinela continua visível e a
  // próxima já é pedida — um observer fixo só avisaria na próxima MUDANÇA
  const canLoad = enabled && items !== null && !loading && !done && !error
  useEffect(() => {
    if (!sentinel || !canLoad) return
    const io = new IntersectionObserver((entries) => {
      if (entries.some((en) => en.isIntersecting)) void load(nextPage.current)
    }, { rootMargin })
    io.observe(sentinel)
    return () => io.disconnect()
  }, [sentinel, canLoad, rootMargin, load])

  const retry = useCallback(() => void load(nextPage.current), [load])

  return { items, last, loading, refreshing, error, done, retry, sentinelRef: setSentinel }
}

/** Fim da lista do scroll infinito: o sentinela observado, "Carregando…"
 *  discreto enquanto a próxima página vem, "Fim dos resultados" quando acaba
 *  e, se a carga falhar, o erro com "Tentar de novo" (o scroll fica parado
 *  até lá em vez de travar calado). A lista vazia e a 1ª carga ficam com a
 *  tela, que tem o próprio texto. */
export function InfiniteScrollFooter<T, R extends InfinitePage<T>>({
  scroll, endText = 'Fim dos resultados',
}: {
  scroll: InfiniteScroll<T, R>
  endText?: string
}) {
  const { items, loading, refreshing, error, done, retry, sentinelRef } = scroll
  if (error) {
    return (
      <div className="flex flex-col items-center gap-2 py-6 text-sm">
        <span className="text-red-400">Erro ao carregar: {error}</span>
        <button
          onClick={retry}
          className="rounded-lg border border-zinc-700 px-4 py-1.5 font-medium text-zinc-200 hover:bg-zinc-800"
        >
          Tentar de novo
        </button>
      </div>
    )
  }
  if (!items?.length) return null
  return (
    <>
      {!done && <div ref={sentinelRef} aria-hidden className="h-px" />}
      <div className="py-6 text-center text-xs text-zinc-500">
        {loading && !refreshing ? 'Carregando…' : done ? endText : ' '}
      </div>
    </>
  )
}
