import { useMemo, useRef, useState, type ReactNode } from 'react'
import type { EdlSegment, RenderPlan, RenderSlice } from '../api'

/** Timeline do alinhamento e do render: duas trilhas (áudio dublado e vídeo
 *  original) com os trechos que o alinhador achou e as quebras entre eles,
 *  e a linha da SAÍDA com as fatias do render e os cortes do vídeo. Passar o
 *  mouse mostra o que foi encontrado naquele trecho / por que o corte
 *  aconteceu. Serve ao vivo (rota de progresso) e na ficha do episódio. */

const SEG_COLOR: Record<string, string> = {
  match: 'bg-emerald-600/80',
  pal: 'bg-sky-600/80',
  drift: 'bg-sky-600/80',
  gap_dub: 'bg-yellow-600/80',
  gap_orig: 'bg-yellow-600/80',
  replaced: 'bg-red-600/90',
}

export const KIND_LABEL: Record<string, string> = {
  match: 'Trecho idêntico',
  pal: 'Speedup PAL',
  drift: 'Drift de framerate',
  gap_dub: 'Só no dublado (descartado)',
  gap_orig: 'Sem dublagem (cena só do original)',
  replaced: 'Cena substituída',
}

const ACTION_LABEL: Record<string, string> = {
  fill_original: 'preenchido com o áudio original',
  silence: 'silêncio',
  use_dub: 'dublagem mantida',
  cut_video: 'cortado do vídeo',
  accept: 'descartado (confirmado)',
}

const SLICE_COLOR: Record<RenderSlice['src'], string> = {
  dub: 'bg-emerald-600/80',
  orig: 'bg-amber-500/80',
  silence: 'bg-zinc-600',
}

const SLICE_LABEL: Record<RenderSlice['src'], string> = {
  dub: 'Dublagem',
  orig: 'Áudio original',
  silence: 'Silêncio',
}

function t(s: number | null | undefined): string {
  if (s == null) return '—'
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const sec = s % 60
  const ss = sec.toFixed(1).padStart(4, '0').replace('.', ',')
  return h ? `${h}:${String(m).padStart(2, '0')}:${ss}` : `${m}:${ss}`
}

/** Offset: em ms quando pequeno (o caso de lip sync); acima de 10 s, em
 *  segundos — "-29450 ms" não se lê de relance. */
function ms(s: number | null | undefined): string {
  if (s == null) return '—'
  const sinal = s >= 0 ? '+' : '−'
  if (Math.abs(s) >= 10) return `${sinal}${Math.abs(s).toFixed(2).replace('.', ',')} s`
  return `${sinal}${Math.abs(Math.round(s * 1000))} ms`
}

interface Tip { x: number; y: number; content: ReactNode }

/** Barra com hover: mapeia a posição do mouse para segundos e pede o
 *  conteúdo do tooltip a `pick`. */
function Bar({ dur, children, pick, onLeave, setTip, className = '' }: {
  dur: number
  children: ReactNode
  pick: (sec: number, pxPerSec: number) => ReactNode | null
  onLeave?: () => void
  setTip: (t: Tip | null) => void
  className?: string
}) {
  const ref = useRef<HTMLDivElement>(null)
  return (
    <div
      ref={ref}
      className={`relative h-5 flex-1 overflow-hidden rounded bg-zinc-800 ${className}`}
      onMouseMove={(e) => {
        const r = ref.current?.getBoundingClientRect()
        if (!r || !dur) return
        const x = e.clientX - r.left
        const content = pick((x / r.width) * dur, r.width / dur)
        // posição relativa ao quadro da timeline (Bar -> Row -> quadro),
        // presa às bordas para o tooltip não sair dele
        const box = ref.current?.parentElement?.parentElement?.getBoundingClientRect() ?? r
        const half = Math.min(170, box.width / 2)
        const px = Math.min(Math.max(e.clientX - box.left, half), box.width - half)
        setTip(content ? { x: px, y: r.top - box.top, content } : null)
      }}
      onMouseLeave={() => { setTip(null); onLeave?.() }}
    >
      {children}
    </div>
  )
}

function pct(v: number, dur: number) {
  return `${Math.max(0, Math.min(100, (v / dur) * 100))}%`
}

function Row({ label, sub, children }: { label: string; sub?: string; children: ReactNode }) {
  return (
    <div className="flex items-center gap-2">
      <div className="w-28 shrink-0 text-xs text-zinc-400">
        {label}
        {sub && <div className="text-[10px] text-zinc-500">{sub}</div>}
      </div>
      {children}
    </div>
  )
}

function segTip(s: EdlSegment, i: number, n: number): ReactNode {
  const refine = s.extra && typeof s.extra.refine === 'string' ? s.extra.refine : null
  const refineTxt = (s as EdlSegment & { refine?: string }).refine ?? refine
  const pic = s.extra?.picture_s
  return (
    <div className="space-y-0.5">
      <div className="font-semibold text-zinc-100">
        {KIND_LABEL[s.kind] ?? s.kind}
        <span className="ml-1.5 font-normal text-zinc-500">#{i + 1}/{n}</span>
      </div>
      <div>Dublado: {t(s.a_start)} – {t(s.a_end)}
        {s.a_end > s.a_start && <span className="text-zinc-500"> ({(s.a_end - s.a_start).toFixed(1)} s)</span>}
      </div>
      <div>Original: {t(s.b_start)} – {t(s.b_end)}
        {s.b_start != null && s.b_end != null && s.b_end > s.b_start
          && <span className="text-zinc-500"> ({(s.b_end - s.b_start).toFixed(1)} s)</span>}
      </div>
      {s.offset != null && <div>Offset: {ms(s.offset)}</div>}
      {['match', 'pal', 'drift'].includes(s.kind) && (
        <div>Confiança: {Math.round((s.confidence ?? 0) * 100)}%</div>
      )}
      {pic != null && s.kind !== 'match' && (
        <div>{pic < 0.5 ? 'Tela preta no original' : `${pic.toFixed(1)} s de imagem no original`}</div>
      )}
      {s.action && <div>Decisão: {ACTION_LABEL[s.action] ?? s.action}</div>}
      {refineTxt && <div className="text-zinc-400">Refino: {refineTxt}</div>}
      {s.note && <div className="text-zinc-400">{s.note}</div>}
    </div>
  )
}

/** Trilha de um lado (a = dublado, b = original). */
function SegTrack({ segs, side, dur, hover, setHover, setTip, scanPct, window }: {
  segs: EdlSegment[] | null
  side: 'a' | 'b'
  dur: number
  hover: number | null
  setHover: (i: number | null) => void
  setTip: (t: Tip | null) => void
  scanPct?: number
  window?: [number, number] | null
}) {
  const spans = useMemo(() => (segs ?? []).map((s, i) => {
    const s0 = side === 'a' ? s.a_start : s.b_start
    const s1 = side === 'a' ? s.a_end : s.b_end
    return { i, s0, s1, s }
  }).filter((x) => x.s0 != null && x.s1 != null && x.s1 > x.s0) as
    { i: number; s0: number; s1: number; s: EdlSegment }[], [segs, side])

  if (!segs) {
    // ainda lendo o arquivo: só o avanço da leitura (os trechos dependem
    // dos DOIS arquivos lidos por inteiro)
    const p = scanPct ?? 0
    return (
      <div className="relative h-5 flex-1 overflow-hidden rounded bg-zinc-800">
        <div className="absolute inset-y-0 left-0 bg-zinc-600/70 transition-all"
          style={{ width: `${p}%` }} />
        <div className="absolute inset-0 flex items-center justify-center text-[10px] text-zinc-300">
          {p >= 100 ? 'lido' : `lendo… ${Math.round(p)}%`}
        </div>
      </div>
    )
  }
  return (
    <Bar
      dur={dur}
      setTip={setTip}
      onLeave={() => setHover(null)}
      pick={(sec) => {
        const hit = spans.find((x) => sec >= x.s0 && sec < x.s1)
        setHover(hit ? hit.i : null)
        return hit ? segTip(hit.s, hit.i, segs.length) : null
      }}
    >
      {spans.map(({ i, s0, s1, s }) => (
        <div
          key={i}
          className={`absolute inset-y-0 ${SEG_COLOR[s.kind] ?? 'bg-zinc-600'} ${
            s.action === 'cut_video' ? 'bg-[repeating-linear-gradient(45deg,transparent_0_3px,rgba(0,0,0,.35)_3px_6px)]' : ''
          } ${hover === i ? 'brightness-150 ring-1 ring-inset ring-white' : ''}`}
          style={{ left: pct(s0, dur), width: `max(1px, ${((s1 - s0) / dur) * 100}%)` }}
        />
      ))}
      {/* quebras: uma marca em cada fronteira entre trechos */}
      {spans.slice(1).map(({ i, s0 }) => (
        <div key={`q${i}`} className="pointer-events-none absolute inset-y-0 w-px bg-zinc-950"
          style={{ left: pct(s0, dur) }} />
      ))}
      {/* arquivo fundido: o que fica fora da janela do episódio, escurecido */}
      {side === 'b' && window && (
        <>
          <div className="pointer-events-none absolute inset-y-0 left-0 bg-black/60"
            style={{ width: pct(window[0], dur) }} />
          <div className="pointer-events-none absolute inset-y-0 right-0 bg-black/60"
            style={{ left: pct(window[1], dur) }} />
        </>
      )}
    </Bar>
  )
}

function sliceTip(sl: RenderSlice, prev: RenderSlice | undefined): ReactNode {
  const mudou = prev && sl.src === 'dub' && prev.src === 'dub'
    && sl.offset != null && prev.offset != null
    && Math.abs(sl.offset - prev.offset) > 0.0005
  return (
    <div className="space-y-0.5">
      <div className="font-semibold text-zinc-100">{SLICE_LABEL[sl.src]}</div>
      <div>Saída: {t(sl.b_start)} – {t(sl.b_end)}
        <span className="text-zinc-500"> ({(sl.b_end - sl.b_start).toFixed(1)} s)</span>
      </div>
      {sl.src === 'dub' && sl.src_start != null && (
        <div>Do dublado a partir de {t(sl.src_start)} · offset {ms(sl.offset)}</div>
      )}
      {sl.tempo && <div>Velocidade ajustada: ×{sl.tempo.toFixed(4)}</div>}
      {sl.why && <div className="text-zinc-300">{sl.why}</div>}
      {mudou && (
        <div className="text-amber-300">
          Quebra no início: offset {ms(prev!.offset)} → {ms(sl.offset)} (edição no áudio)
        </div>
      )}
      {prev && prev.src !== sl.src && (
        <div className="text-amber-300">
          Quebra no início: {SLICE_LABEL[prev.src].toLowerCase()} → {SLICE_LABEL[sl.src].toLowerCase()}
        </div>
      )}
      {sl.stretch ? (
        <div className="text-zinc-500">
          Esticada {(sl.stretch * 1000).toFixed(0)} ms para cobrir sobra de fronteira
        </div>
      ) : null}
    </div>
  )
}

function PlanTrack({ plan, setTip }: { plan: RenderPlan; setTip: (t: Tip | null) => void }) {
  const dur = plan.duration || 1
  // fronteiras que importam: troca de fonte ou de offset entre fatias
  const quebras = plan.slices.slice(1).filter((sl, k) => {
    const prev = plan.slices[k]
    return prev.src !== sl.src || (sl.src === 'dub' && sl.offset != null && prev.offset != null
      && Math.abs(sl.offset - prev.offset) > 0.0005)
  })
  return (
    <Bar
      dur={dur}
      setTip={setTip}
      pick={(sec, pxPerSec) => {
        // corte do vídeo perto do mouse (alguns px) ganha do trecho
        const tol = 5 / Math.max(pxPerSec, 1e-9)
        const corte = plan.cuts.find((c) => Math.abs(c.at - sec) <= tol)
        if (corte) {
          return (
            <div className="space-y-0.5">
              <div className="font-semibold text-red-300">Corte no vídeo</div>
              <div>Na saída: {t(corte.at)}</div>
              <div>Removido do original: {t(corte.src_start)} – {t(corte.src_end)}
                <span className="text-zinc-500"> ({(corte.src_end - corte.src_start).toFixed(1)} s)</span>
              </div>
              <div className="text-zinc-300">{corte.why}</div>
            </div>
          )
        }
        const k = plan.slices.findIndex((sl) => sec >= sl.b_start && sec < sl.b_end)
        return k >= 0 ? sliceTip(plan.slices[k], plan.slices[k - 1]) : null
      }}
    >
      {plan.slices.map((sl, k) => (
        <div key={k} className={`absolute inset-y-0 ${SLICE_COLOR[sl.src]}`}
          style={{ left: pct(sl.b_start, dur), width: `max(1px, ${((sl.b_end - sl.b_start) / dur) * 100}%)` }} />
      ))}
      {quebras.map((sl, k) => (
        <div key={`q${k}`} className="pointer-events-none absolute inset-y-0 w-px bg-zinc-950"
          style={{ left: pct(sl.b_start, dur) }} />
      ))}
      {plan.cuts.map((c, k) => (
        <div key={`c${k}`} className="pointer-events-none absolute -inset-y-0 w-0.5 bg-red-500"
          style={{ left: pct(c.at, dur) }} />
      ))}
    </Bar>
  )
}

function Axis({ dur }: { dur: number }) {
  if (!dur) return null
  const passo = [60, 120, 300, 600, 900, 1800].find((p) => dur / p <= 10) ?? 3600
  const marcas: number[] = []
  for (let s = 0; s <= dur; s += passo) marcas.push(s)
  return (
    <div className="flex items-center gap-2">
      <div className="w-28 shrink-0" />
      <div className="relative h-3 flex-1 text-[10px] text-zinc-500">
        {marcas.map((s) => (
          <span key={s} className="absolute -translate-x-1/2 tabular-nums" style={{ left: pct(s, dur) }}>
            {Math.round(s / 60)}min
          </span>
        ))}
      </div>
    </div>
  )
}

export function AlignTimeline({ segments, durDub, durOrig, scan, plan, window, stage }: {
  segments: EdlSegment[] | null | undefined
  durDub?: number | null
  durOrig?: number | null
  scan?: { dub?: number; orig?: number }
  plan?: RenderPlan | null
  window?: [number, number] | null
  stage?: string | null
}) {
  const [hover, setHover] = useState<number | null>(null)
  const [tip, setTip] = useState<Tip | null>(null)
  const segs = segments ?? null
  const dA = durDub || Math.max(1, ...(segs ?? []).map((s) => s.a_end))
  const dB = durOrig || Math.max(1, ...(segs ?? []).map((s) => s.b_end ?? 0))
  const counts = useMemo(() => {
    const c: Record<string, number> = {}
    for (const s of segs ?? []) c[s.kind] = (c[s.kind] ?? 0) + 1
    return c
  }, [segs])

  return (
    <div className="relative rounded-xl border border-zinc-800 bg-zinc-950/40 p-3">
      {/* o tooltip fica fora do grupo espaçado: no Tailwind 4 o space-y põe
          margem embaixo de todo filho menos o último, e o tooltip virando o
          último filho fazia a legenda ganhar margem e a caixa crescer */}
      <div className="space-y-1.5">
      {stage && <div className="mb-1 text-xs text-zinc-400">{stage}</div>}
      <Row label="Áudio dublado" sub={durDub ? t(durDub) : undefined}>
        <SegTrack segs={segs} side="a" dur={dA} hover={hover} setHover={setHover}
          setTip={setTip} scanPct={scan?.dub} />
      </Row>
      <Row label="Vídeo original" sub={durOrig ? t(durOrig) : undefined}>
        <SegTrack segs={segs} side="b" dur={dB} hover={hover} setHover={setHover}
          setTip={setTip} scanPct={scan?.orig} window={window} />
      </Row>
      {/* sem trechos ainda (lendo os arquivos) a duração não é conhecida */}
      {segs && <Axis dur={dB} />}
      {plan && (
        <>
          <Row label="Saída (render)" sub={t(plan.duration)}>
            <PlanTrack plan={plan} setTip={setTip} />
          </Row>
          <Axis dur={plan.duration} />
        </>
      )}

      {/* legenda */}
      <div className="flex flex-wrap gap-x-3 gap-y-1 pt-1 text-[11px] text-zinc-400">
        {segs && Object.entries(counts).map(([k, n]) => (
          <span key={k} className="inline-flex items-center gap-1">
            <span className={`h-2.5 w-2.5 rounded-sm ${SEG_COLOR[k] ?? 'bg-zinc-600'}`} />
            {KIND_LABEL[k] ?? k} ({n})
          </span>
        ))}
        {plan && (
          <>
            {(['dub', 'orig', 'silence'] as const)
              .filter((src) => plan.slices.some((s) => s.src === src))
              .map((src) => (
                <span key={src} className="inline-flex items-center gap-1">
                  <span className={`h-2.5 w-2.5 rounded-sm ${SLICE_COLOR[src]}`} />
                  Saída: {SLICE_LABEL[src].toLowerCase()}
                </span>
              ))}
            {plan.cuts.length > 0 && (
              <span className="inline-flex items-center gap-1">
                <span className="h-2.5 w-0.5 bg-red-500" /> {plan.cuts.length} corte(s) no vídeo
              </span>
            )}
          </>
        )}
      </div>
      </div>

      {tip && (
        <div
          className="pointer-events-none absolute z-10 w-max max-w-[340px] -translate-x-1/2 -translate-y-full rounded-lg border border-zinc-700 bg-zinc-900/95 px-2.5 py-1.5 text-xs text-zinc-300 shadow-xl"
          style={{ left: tip.x, top: tip.y - 6 }}
        >
          {tip.content}
        </div>
      )}
    </div>
  )
}
