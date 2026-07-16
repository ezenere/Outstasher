import { useEffect, useRef, useState, type ReactNode } from 'react'
import type { Candidate, ConvertOptions, DiskInfo, MergeProgress, MovieState, Progress, QbitTone } from '../api'
import {
  convertSummary, fmtDisk, fmtEta, fmtSize, fmtSpeed, fmtTime, langName,
  MOVIE_STATE_LABEL, STATUS_LABEL, qbitIsComplete, qbitState,
} from '../api'
import { BookmarkSolid, Check, MediaVideoList, Download, Search, Timer, WarningTriangle, CheckCircle, XmarkCircle } from 'iconoir-react'

const BADGE_STYLES: Record<string, string> = {
  searching: 'bg-blue-950 text-blue-400',
  awaiting: 'bg-purple-950 text-purple-400',
  downloading: 'bg-yellow-950 text-yellow-400',
  merging: 'bg-blue-950 text-blue-400',
  done: 'bg-emerald-950 text-emerald-400',
  error: 'bg-red-950 text-red-400',
  cancelled: 'bg-zinc-800 text-zinc-400',
}

export function Badge({ status }: { status: string }) {
  return (
    <span className={`rounded-full px-2.5 py-0.5 text-xs font-semibold whitespace-nowrap ${BADGE_STYLES[status] ?? 'bg-zinc-800 text-zinc-300'}`}>
      {STATUS_LABEL[status] ?? status}
    </span>
  )
}

const QBIT_TONE_BAR: Record<QbitTone, string> = {
  ok: 'bg-blue-500',
  warn: 'bg-amber-500',
  done: 'bg-emerald-500',
  err: 'bg-red-500',
  neutral: 'bg-zinc-500',
}
const QBIT_TONE_TEXT: Record<QbitTone, string> = {
  ok: 'text-blue-300',
  warn: 'text-amber-300',
  done: 'text-emerald-300',
  err: 'text-red-300',
  neutral: 'text-zinc-400',
}

export function ProgressBar({ label, p }: { label: string; p: Progress | null }) {
  if (!p) return null
  const complete = qbitIsComplete(p.state) || (p.pct || 0) >= 100
  const st = qbitState(p.state)
  // torrent concluído: barra verde, sem velocidade/ETA — só o estado (seedando)
  const tone = complete ? 'done' : st.tone
  const size = p.size
    ? complete
      ? fmtSize(p.size)
      : `${fmtSize(p.downloaded ?? 0)} / ${fmtSize(p.size)}`
    : null
  const extra = complete
    ? [size, p.seeds != null ? `${p.seeds} seeds` : null]
    : [
        size,
        p.speed ? fmtSpeed(p.speed) : null,
        p.eta != null && p.eta < 8640000 && (p.pct || 0) < 100 ? `ETA ${fmtEta(p.eta)}` : null,
        p.seeds != null ? `${p.seeds} seeds` : null,
      ]
  const extraStr = extra.filter(Boolean).join(' · ')
  return (
    <div className="mt-2">
      <div className="h-2 overflow-hidden rounded bg-zinc-800">
        <div
          className={`h-full transition-all duration-500 ${QBIT_TONE_BAR[tone]}`}
          style={{ width: `${complete ? 100 : p.pct || 0}%` }}
        />
      </div>
      <div className="mt-1 flex flex-wrap items-center gap-x-1.5 text-xs text-zinc-400">
        <span className="font-medium text-zinc-300">{label}</span>
        {complete ? (
          <span className="inline-flex items-center gap-0.5 text-emerald-300">
            <Check width={13} height={13} /> Concluído
          </span>
        ) : (
          <span className="tabular-nums">{p.pct || 0}%</span>
        )}
        {st.label && <span className={QBIT_TONE_TEXT[tone]}>· {st.label}</span>}
        {extraStr && <span className="text-zinc-500">· {extraStr}</span>}
      </div>
    </div>
  )
}

/** Barra de progresso da conversão (ffmpeg): tempo do filme, velocidade, tamanho... */
export function MergeBar({ p }: { p?: MergeProgress | null }) {
  if (!p) return null
  const write = p.pct || 0                      // barra da frente: já codificado (escrito)
  const read = Math.max(write, p.read_pct ?? write)  // barra de trás: já lido pelo encoder
  // gap visível = frames no buffer do encoder (grande em AV1, ~0 em H264/HEVC)
  const buffering = read - write > 1
  const extra = [
    p.duration_s ? `${fmtTime(p.out_s, true)} / ${fmtTime(p.duration_s, true)}` : fmtTime(p.out_s, true),
    p.speed ? `${p.speed.toFixed(2)}x` : null,
    p.fps ? `${Math.round(p.fps)} fps` : null,
    p.size ? fmtSize(p.size) : null,
    p.bitrate ? `${(p.bitrate / 1e6).toFixed(1)} Mb/s` : null,
    p.eta != null && write < 100 ? `ETA ${fmtEta(p.eta)}` : null,
  ]
    .filter(Boolean)
    .join(' · ')
  return (
    <div className="mt-2">
      {/* duas barras sobrepostas: lidos (claro, atrás) e escritos (forte, frente) */}
      <div className="relative h-2 overflow-hidden rounded bg-zinc-800">
        <div
          className="absolute inset-y-0 left-0 bg-purple-500/30 transition-all duration-500"
          style={{ width: `${read}%` }}
        />
        <div
          className="absolute inset-y-0 left-0 bg-purple-500 transition-all duration-500"
          style={{ width: `${write}%` }}
        />
      </div>
      <div className="mt-1 text-xs text-zinc-400">
        Conversão: {write}%
        {buffering && <span className="text-purple-300/80"> (lidos {Math.round(read)}%)</span>}
        {extra ? ` — ${extra}` : ''}
      </div>
    </div>
  )
}

/** Tempo decorrido desde `since` (ISO). Enquanto `running`, atualiza a cada
 *  segundo; parado, congela no último valor. Usado no tempo de conversão/cópia. */
export function Elapsed({ since, running, title }: {
  since: string
  running: boolean
  title?: string
}) {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!running) return
    setNow(Date.now())  // atualiza já ao (re)entrar em execução
    const t = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(t)
  }, [running])
  const start = new Date(since).getTime()
  if (Number.isNaN(start)) return null
  const secs = Math.max(0, (now - start) / 1000)
  return (
    <span className="inline-flex items-center gap-1 text-xs text-zinc-400" title={title}>
      <Timer width={13} height={13} /> {fmtEta(secs)}
    </span>
  )
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="py-3 text-zinc-500">{children}</div>
}

/** Texto que colapsa em 1 linha (ellipsis) quando transborda, com um
 *  "mostrar mais / menos" à direita. Útil para o detail do job quando ele é o
 *  comando ffmpeg inteiro. O botão só aparece se o texto realmente transbordar. */
export function ClampText({ children, className = '' }: {
  children: string
  className?: string
}) {
  const [expanded, setExpanded] = useState(false)
  const [overflows, setOverflows] = useState(false)
  const boxRef = useRef<HTMLDivElement>(null)
  const mirrorRef = useRef<HTMLSpanElement>(null)
  // Mede num espelho fora da tela, não no span visível: expandido ele não trunca
  // mais (scrollWidth == clientWidth) e a medição se perderia. Compara a largura
  // real do texto com a do container; o ResizeObserver remede no resize.
  useEffect(() => {
    const box = boxRef.current
    const mirror = mirrorRef.current
    if (!box || !mirror) return
    const measure = () => setOverflows(mirror.scrollWidth > box.clientWidth + 1)
    measure()
    const ro = new ResizeObserver(measure)
    ro.observe(box)
    return () => ro.disconnect()
  }, [children])
  return (
    <div ref={boxRef} className={`relative ${className}`}>
      <div className="flex items-start gap-2">
        <span className={`min-w-0 flex-1 ${expanded ? 'whitespace-pre-wrap wrap-break-word' : 'truncate whitespace-nowrap'}`}>
          {children}
        </span>
        {overflows && (
          <button
            type="button"
            onClick={(e) => {
              e.stopPropagation()  // o card em volta pode ser clicável
              setExpanded((v) => !v)
            }}
            className="shrink-0 whitespace-nowrap text-xs text-zinc-500 hover:text-zinc-300"
          >
            {expanded ? 'mostrar menos' : 'mostrar mais'}
          </button>
        )}
      </div>
      {/* espelho invisível, largura livre: dá o tamanho real do texto em 1 linha */}
      <span
        ref={mirrorRef}
        aria-hidden
        className="pointer-events-none invisible absolute block h-0 w-max whitespace-nowrap"
      >
        {children}
      </span>
    </div>
  )
}

/** Tag colorida com tooltip. Cor por tipo (o `title` detalha o significado). */
export function Tag({ tone, title, children }: {
  tone: 'lang' | 'orig' | 'custom' | 'muted' | 'info'
  title: string
  children: ReactNode
}) {
  const cls = {
    lang: 'bg-blue-950 text-blue-300',
    orig: 'bg-amber-950 text-amber-300',
    custom: 'bg-purple-950 text-purple-300',
    info: 'bg-sky-950 text-sky-300',
    muted: 'bg-zinc-800 text-zinc-400',
  }[tone]
  return (
    <span title={title} className={`rounded px-1.5 py-0.5 text-xs font-medium ${cls}`}>
      {children}
    </span>
  )
}

interface KindTagsProps {
  kind?: string
  language: string
  downloadOnly?: boolean
  convert?: ConvertOptions | boolean | null
  mode?: string
}

/** Tags do tipo do job: idioma dublado, original, conversão custom, só baixar,
 *  modo manual/arquivos locais. Substitui o antigo texto "pt + orig". */
export function KindTags({ kind, language, downloadOnly, convert, mode }: KindTagsProps) {
  const lang = langName(language)
  // convert pode vir como flag (lista) ou objeto completo (detalhe) — no 2º caso
  // o tooltip lista as opções escolhidas
  const convSummary = typeof convert === 'object' && convert ? convertSummary(convert) : []
  const convTitle = convSummary.length
    ? `Conversão customizada: ${convSummary.join(', ')}`
    : 'Conversão customizada (opções avançadas)'
  return (
    <span className="inline-flex flex-wrap items-center gap-1 align-middle">
      {kind !== 'original' && (
        <Tag tone="lang" title={`Áudio dublado em ${lang}`}>{lang}</Tag>
      )}
      {kind !== 'dubbed' && (
        <Tag tone="orig" title="Vídeo no idioma original">Original</Tag>
      )}
      {downloadOnly && (
        <Tag tone="muted" title="Só baixa pelo qBittorrent — sem merge/conversão">Só baixar</Tag>
      )}
      {convert && (
        <Tag tone="custom" title={convTitle}>Custom</Tag>
      )}
      {mode === 'manual' && (
        <Tag tone="muted" title="Torrents escolhidos manualmente">Manual</Tag>
      )}
      {mode === 'files' && (
        <Tag tone="muted" title="Conversão de arquivos locais">Arquivos locais</Tag>
      )}
    </span>
  )
}

/** Bloco recolhível: cabeçalho clicável (▸/▾) + conteúdo escondido por padrão.
 *  Para informação secundária que não precisa ficar à vista o tempo todo. */
export function Collapsible({
  title, children, defaultOpen = false, right, flush = false,
}: {
  title: ReactNode
  children: ReactNode
  defaultOpen?: boolean
  right?: ReactNode
  /** Cola o conteúdo no container: sem padding, borda reta em baixo (para tabelas). */
  flush?: boolean
}) {
  const [open, setOpen] = useState(defaultOpen)
  return (
    <div className={`border border-zinc-800 ${flush && open ? 'rounded-t-xl' : 'rounded-xl'}`}>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-2 px-3 py-2 text-left text-sm font-medium text-zinc-300 hover:text-zinc-100"
      >
        <span className="text-xs text-zinc-500">{open ? '▾' : '▸'}</span>
        <span className="flex-1">{title}</span>
        {right}
      </button>
      {open && <div className={`border-t border-zinc-800 ${flush ? '' : 'p-3'}`}>{children}</div>}
    </div>
  )
}

// ícone + cor por estado do filme (usado no card de filme e no dropdown)
const STATE_STYLE: Record<MovieState, { icon: typeof Download; cls: string; badge: string }> = {
  converting: { icon: MediaVideoList, cls: 'text-blue-300', badge: 'bg-blue-950/80 text-blue-300' },
  downloading: { icon: Download, cls: 'text-yellow-300', badge: 'bg-yellow-950/80 text-yellow-300' },
  searching: { icon: Search, cls: 'text-sky-300', badge: 'bg-sky-950/80 text-sky-300' },
  awaiting: { icon: WarningTriangle, cls: 'text-purple-300', badge: 'bg-purple-950/80 text-purple-300' },
  done: { icon: CheckCircle, cls: 'text-emerald-300', badge: 'bg-emerald-950/80 text-emerald-300' },
  error: { icon: XmarkCircle, cls: 'text-red-300', badge: 'bg-red-950/80 text-red-300' },
}

/** Ícone do estado do filme (para sobrepor no poster). */
export function MovieStateIcon({ state, className }: { state: MovieState; className?: string }) {
  const { icon: Icon, cls } = STATE_STYLE[state]
  return <Icon width={16} height={16} className={`${cls} ${className ?? ''}`} />
}

/** Badge de estado com ícone + rótulo. */
export function MovieStateBadge({ state }: { state: MovieState }) {
  const { icon: Icon, badge } = STATE_STYLE[state]
  return (
    <span className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-semibold ${badge}`}>
      <Icon width={12} height={12} /> {MOVIE_STATE_LABEL[state]}
    </span>
  )
}

/** Barra de uso do disco: [======      ] 650 GB / 1 TB · 350 GB livres */
export function DiskBar({ disk }: { disk?: DiskInfo | null }) {
  if (!disk || !disk.total) {
    return <div className="text-xs text-zinc-600">disco indisponível (caminho não existe nesta máquina)</div>
  }
  const pct = Math.min(100, Math.round((disk.used / disk.total) * 100))
  const tight = pct >= 90
  return (
    <div>
      <div className="h-2 overflow-hidden rounded bg-zinc-800">
        <div
          className={`h-full transition-all ${tight ? 'bg-red-500' : pct >= 75 ? 'bg-yellow-500' : 'bg-emerald-500'}`}
          style={{ width: `${pct}%` }}
        />
      </div>
      <div className="mt-1 text-xs text-zinc-400">
        {fmtDisk(disk.used)} / {fmtDisk(disk.total)}
        {' · '}
        <span className={tight ? 'text-red-400' : 'text-zinc-300'}>{fmtDisk(disk.free)} livres</span>
      </div>
    </div>
  )
}

/** Espaço livre compacto para exibir ao lado de um seletor. */
export function DiskFree({ disk }: { disk?: DiskInfo | null }) {
  if (!disk || !disk.total) return null
  const tight = disk.free / disk.total < 0.1
  return (
    <span className={`text-xs whitespace-nowrap ${tight ? 'text-red-400' : 'text-zinc-500'}`}>
      ({fmtDisk(disk.free)} livre{tight ? ' ⚠' : ''})
    </span>
  )
}

interface CandidatesTableProps {
  candidates: Candidate[]
  selectable?: boolean
  selectedId?: string
  onSelect?: (id: string) => void
  showReason?: boolean
  /** Torrent em uso agora: marca a linha com o bookmark. Casa por `id` (único);
   *  sem id, cai para título + tracker para não marcar releases homônimos de
   *  outros trackers. */
  current?: { id?: string | null; title: string; tracker?: string | null } | null
  /** Cola a tabela no container-pai (sem margem/borda própria — ver Collapsible flush). */
  flush?: boolean
}

export function CandidatesTable({
  candidates, selectable, selectedId, onSelect, showReason, current, flush,
}: CandidatesTableProps) {
  if (!candidates.length)
    return <div className={flush ? 'px-3 py-2' : ''}><Empty>Nenhum candidato.</Empty></div>
  const matchesCurrent = (c: Candidate) => {
    if (!current) return false
    if (current.id && c.id) return c.id === current.id     // id é único: match exato
    return c.title === current.title && (c.tracker ?? null) === (current.tracker ?? null)
  }
  return (
    <div className={`max-h-72 overflow-auto ${flush ? 'rounded-b-xl' : 'mb-2 rounded-lg border border-zinc-800'}`}>
      <table className="w-full border-collapse text-xs">
        <thead>
          <tr className="sticky top-0 bg-zinc-900 text-left text-zinc-400">
            <th className="px-2 py-1.5" />
            <th className="px-2 py-1.5">Título</th>
            <th className="px-2 py-1.5">Tracker</th>
            <th className="px-2 py-1.5">Seeds</th>
            <th className="px-2 py-1.5">Tamanho</th>
            <th className="px-2 py-1.5">Corte</th>
            <th className="px-2 py-1.5">Score</th>
            {showReason && <th className="px-2 py-1.5">Motivo</th>}
          </tr>
        </thead>
        <tbody>
          {candidates.map((c, i) => {
            const isCurrent = matchesCurrent(c)
            const rowCls = c.chosen
              ? 'text-emerald-400 font-semibold'
              : c.rejected
                ? 'text-zinc-500'
                : ''
            const clickable = selectable && c.id
            return (
              <tr
                key={c.id ?? i}
                className={`border-t border-zinc-800/60 ${rowCls} ${clickable ? 'cursor-pointer hover:bg-zinc-800/40' : ''} ${
                  selectable && selectedId === c.id ? 'bg-blue-950/40' : isCurrent ? 'bg-emerald-950/30' : ''
                }`}
                onClick={clickable ? () => onSelect?.(c.id!) : undefined}
              >
                <td className="px-2 py-1.5">
                  {selectable && c.id ? (
                    <input type="radio" checked={selectedId === c.id} onChange={() => onSelect?.(c.id!)} />
                  ) : c.chosen ? (
                    <Check width={14} height={14} className="text-emerald-400" />
                  ) : null}
                </td>
                <td className="max-w-96 truncate px-2 py-1.5" title={c.title}>
                  {isCurrent && (
                    <span title="Torrent em uso agora" className="mr-1 inline-block align-[-1px] text-rose-400">
                      <BookmarkSolid width={13} height={13} />
                    </span>
                  )}
                  {c.year_match && (
                    <span
                      className="mr-1 rounded bg-sky-500/15 px-1 py-px text-[10px] font-medium text-sky-400"
                      title="Nome traz o ano do filme: identificação confiável — tem prioridade sobre releases sem ano"
                    >
                      ano
                    </span>
                  )}
                  {c.title}
                </td>
                <td className="px-2 py-1.5">{c.tracker ?? ''}</td>
                <td className="px-2 py-1.5">{c.seeders}</td>
                <td className="px-2 py-1.5">{fmtSize(c.size)}</td>
                <td className="px-2 py-1.5">{c.edition ?? 'normal'}</td>
                <td className="px-2 py-1.5">{c.score ?? '—'}</td>
                {showReason && (
                  <td className="px-2 py-1.5">{c.rejected ?? (c.chosen ? 'escolhido' : 'ok')}</td>
                )}
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}
