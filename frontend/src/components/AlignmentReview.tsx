import { useEffect, useMemo, useState } from 'react'
import { Check, WarningTriangle } from 'iconoir-react'
import {
  fetchFrame, resolveGate,
  type Edl, type EdlSegment, type Job, type ReviewAction, type ReviewRule,
} from '../api'
import { useDialog } from './Dialog'
import { Collapsible } from './ui'

/** Revisão de alinhamento: timeline de duas faixas por episódio, preview de
 *  frames nas fronteiras e decisão por segmento — com "aplicar como regra a
 *  todos os episódios" para as decisões estruturais (recap, créditos). */

const SEG_COLOR: Record<string, string> = {
  match: 'bg-emerald-600/70',
  pal: 'bg-sky-600/70',
  drift: 'bg-sky-600/70',
  gap_dub: 'bg-yellow-600/80',
  gap_orig: 'bg-yellow-600/80',
  replaced: 'bg-red-600/90',
}

const KIND_LABEL: Record<string, string> = {
  match: 'trecho idêntico',
  pal: 'speedup PAL',
  drift: 'drift de framerate',
  gap_dub: 'só no dublado (recap/abertura?)',
  gap_orig: 'sem dublagem (cena extra do original)',
  replaced: 'cena substituída',
}

// ações possíveis por tipo de segmento (gap_dub é sempre descartado — a
// timeline final é a do original; "accept" só confirma que foi visto)
const ACTIONS_FOR: Record<string, { value: ReviewAction; label: string }[]> = {
  gap_orig: [
    { value: 'fill_original', label: 'Preencher com áudio original' },
    { value: 'silence', label: 'Silêncio' },
    { value: 'cut_video', label: 'Cortar do vídeo (a cena some)' },
  ],
  replaced: [
    { value: 'fill_original', label: 'Áudio original (seguro)' },
    { value: 'use_dub', label: 'Usar a dublagem mesmo assim' },
    { value: 'silence', label: 'Silêncio' },
    { value: 'cut_video', label: 'Cortar do vídeo (a cena some)' },
  ],
  gap_dub: [{ value: 'accept', label: 'Ok, descartar este trecho' }],
}

const DECIDABLE = new Set(['gap_orig', 'gap_dub', 'replaced'])

function fmtT(s: number): string {
  const m = Math.floor(s / 60)
  return `${m}:${String(Math.floor(s % 60)).padStart(2, '0')}`
}

function positionOf(seg: EdlSegment, dur: number): 'start' | 'end' | 'middle' {
  if (seg.a_start <= dur * 0.15) return 'start'
  if (seg.a_end >= dur * 0.85) return 'end'
  return 'middle'
}

/** Barra de uma timeline (a ou b) com os segmentos coloridos clicáveis. */
function Track({ edl, side, selected, onPick }: {
  edl: Edl
  side: 'a' | 'b'
  selected: number | null
  onPick: (idx: number) => void
}) {
  const dur = side === 'a' ? edl.source_dub.duration : edl.source_orig.duration
  return (
    <div className="relative h-5 overflow-hidden rounded bg-zinc-800">
      {edl.segments.map((s, i) => {
        const start = side === 'a' ? s.a_start : s.b_start
        const end = side === 'a' ? s.a_end : s.b_end
        if (start == null || end == null || end <= start) return null
        const left = (start / dur) * 100
        const width = Math.max(0.4, ((end - start) / dur) * 100)
        return (
          <button
            key={i}
            title={`${KIND_LABEL[s.kind]} · ${fmtT(start)}–${fmtT(end)}`}
            onClick={() => DECIDABLE.has(s.kind) && onPick(i)}
            className={`absolute inset-y-0 ${SEG_COLOR[s.kind] ?? 'bg-zinc-600'} ${
              DECIDABLE.has(s.kind) ? 'cursor-pointer hover:brightness-125' : 'cursor-default'
            } ${selected === i ? 'ring-2 ring-white' : ''}`}
            style={{ left: `${left}%`, width: `${width}%` }}
          />
        )
      })}
    </div>
  )
}

/** Preview 2x2: um frame de cada lado, antes e depois da fronteira. */
function FramePreview({ jobId, episode, seg }: {
  jobId: string
  episode: string
  seg: EdlSegment
}) {
  const six = seg.kind === 'replaced'
  const [urls, setUrls] = useState<(string | null)[]>(
    Array<string | null>(six ? 6 : 4).fill(null))
  // chave por VALOR: o detalhe do job recarrega a cada 5 s e cada reload cria
  // um objeto `seg` novo — se o efeito dependesse do objeto, os frames seriam
  // buscados de novo a cada tick. Só tempos/tipo mudam de verdade.
  const segKey = `${seg.kind}|${seg.a_start}|${seg.a_end}|${seg.b_start}|${seg.b_end}`
  useEffect(() => {
    let dead = false
    const ta = Math.max(0, seg.a_start)
    const tb = Math.max(0, seg.b_start ?? seg.a_start)
    // fronteiras (antes/depois) — nas duas devem ser IGUAIS entre dublado e
    // original se o alinhamento está certo. Em "cena substituída" entra também
    // o MEIO do trecho: é aí que o conteúdo diverge (ou não — falso positivo)
    const specs: ['a' | 'b', number][] = [
      ['a', Math.max(0, ta - 1)], ['a', seg.a_end + 1],
      ['b', Math.max(0, tb - 1)], ['b', (seg.b_end ?? tb) + 1],
    ]
    if (seg.kind === 'replaced') {
      specs.splice(1, 0, ['a', (seg.a_start + seg.a_end) / 2])
      specs.splice(4, 0, ['b', ((seg.b_start ?? ta) + (seg.b_end ?? ta)) / 2])
    }
    const fetched: string[] = []
    Promise.all(specs.map(async ([side, t]) => {
      try {
        const u = await fetchFrame(jobId, episode, side, t)
        fetched.push(u)
        return u
      } catch {
        return null
      }
    })).then((us) => {
      if (!dead) setUrls(us)
    })
    return () => {
      dead = true
      fetched.forEach((u) => URL.revokeObjectURL(u))
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jobId, episode, segKey])
  const labels = six
    ? ['dublado (antes)', 'dublado (MEIO)', 'dublado (depois)',
       'original (antes)', 'original (MEIO)', 'original (depois)']
    : ['dublado (antes)', 'dublado (depois)', 'original (antes)', 'original (depois)']
  return (
    <div className={`mt-2 grid grid-cols-2 gap-1.5 ${six ? 'sm:grid-cols-3' : 'sm:grid-cols-4'}`}>
      {urls.map((u, i) => (
        <figure key={i} className="min-w-0">
          {u ? (
            <img src={u} className="w-full rounded bg-zinc-800" alt={labels[i]} />
          ) : (
            <div className="flex aspect-video items-center justify-center rounded bg-zinc-800 text-xs text-zinc-600">…</div>
          )}
          <figcaption className="mt-0.5 truncate text-center text-[10px] text-zinc-500">{labels[i]}</figcaption>
        </figure>
      ))}
    </div>
  )
}

export default function AlignmentReview({ job, onResolved }: {
  job: Job
  onResolved: () => void
}) {
  const dialog = useDialog()
  const episodes = useMemo(
    () => (job.awaiting?.payload as { episodes?: Record<string, Edl> })?.episodes ?? {},
    [job.awaiting])
  const [busy, setBusy] = useState(false)
  // decisões: por episódio -> índice do segmento -> ação
  const [actions, setActions] = useState<Record<string, Record<number, ReviewAction>>>({})
  const [rules, setRules] = useState<ReviewRule[]>([])
  const [skip, setSkip] = useState<Set<string>>(new Set())
  const [sel, setSel] = useState<{ ep: string; idx: number } | null>(null)
  // "aplicar como regra": guarda a intenção para a PRÓXIMA ação escolhida
  const [asRule, setAsRule] = useState(false)

  // trechos que pedem decisão, na ordem do episódio (a "listinha")
  function decidable(ep: string): number[] {
    return episodes[ep].segments
      .map((s, i) => (DECIDABLE.has(s.kind) ? i : -1))
      .filter((i) => i >= 0)
  }

  function step(ep: string, from: number, dir: 1 | -1): number | null {
    const list = decidable(ep)
    const pos = list.indexOf(from)
    const next = list[pos + dir]
    return next === undefined ? null : next
  }

  function decide(ep: string, idx: number, action: ReviewAction) {
    setActions((cur) => ({ ...cur, [ep]: { ...(cur[ep] ?? {}), [idx]: action } }))
    // decidiu -> vai para o próximo trecho automaticamente (fica no último)
    const nxt = step(ep, idx, 1)
    if (nxt !== null) setSel({ ep, idx: nxt })
    if (asRule) {
      const seg = episodes[ep].segments[idx]
      const dur = Math.max(seg.a_end - seg.a_start,
        (seg.b_end ?? 0) - (seg.b_start ?? 0))
      setRules((cur) => [...cur, {
        when: {
          kind: seg.kind,
          position: positionOf(seg, episodes[ep].source_dub.duration),
          min_len: Math.max(0.5, dur * 0.5),
          max_len: dur * 1.5,
        },
        action,
      }])
    }
  }

  // um episódio está resolvido quando todo `replaced` dele tem ação (as
  // regras recém-criadas também contam — o backend as reaplica)
  function unresolved(ep: string): number[] {
    const edl = episodes[ep]
    return edl.segments
      .map((s, i) => ({ s, i }))
      .filter(({ s, i }) => s.kind === 'replaced' && !s.action
        && !actions[ep]?.[i]
        && !rules.some((r) => !r.when.kind || r.when.kind === s.kind))
      .map(({ i }) => i)
  }

  async function submit() {
    if (busy) return
    const pending = Object.keys(episodes)
      .filter((k) => !skip.has(k) && unresolved(k).length > 0)
    if (pending.length && !(await dialog.confirm({
      title: 'Revisão incompleta',
      message: `${pending.length} episódio(s) ainda têm cena substituída sem decisão e vão CONTINUAR pausados: ${pending.join(', ')}. Enviar mesmo assim?`,
      confirmText: 'Enviar',
    }))) return
    setBusy(true)
    try {
      await resolveGate(job.id, 'alignment_review', {
        actions: Object.fromEntries(Object.entries(actions).map(
          ([ep, m]) => [ep, Object.fromEntries(
            Object.entries(m).map(([i, a]) => [String(i), a]))])),
        rules,
        skip: [...skip],
      })
      onResolved()
    } catch (e) {
      await dialog.alert({ title: 'Erro', message: (e as Error).message })
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="mt-6 rounded-xl border border-amber-900/60 bg-amber-950/20 p-4">
      <h2 className="mb-2 flex items-center gap-1.5 font-semibold text-amber-300">
        <WarningTriangle width={16} height={16} /> Revisão de alinhamento
      </h2>
      <p className="text-sm text-zinc-300">
        O alinhador encontrou trechos que exigem a sua decisão (verde = igual,
        amarelo = falta/sobra de material, <span className="text-red-400">vermelho = cena substituída</span>).
        Clique num trecho colorido para comparar os frames e decidir.
      </p>
      <label className="mt-2 flex items-center gap-1.5 text-sm text-zinc-300">
        <input type="checkbox" checked={asRule} onChange={(e) => setAsRule(e.target.checked)} />
        Aplicar as próximas decisões como REGRA a todos os episódios
        (mesmo tipo, mesma região, duração parecida)
      </label>

      <div className="mt-3 space-y-2">
        {Object.entries(episodes).map(([key, edl]) => {
          const nUnresolved = skip.has(key) ? 0 : unresolved(key).length
          return (
            <Collapsible
              key={key}
              title={`${key} — ${edl.segments.length} trecho(s)`}
              right={nUnresolved
                ? <span className="text-xs text-red-400">{nUnresolved} sem decisão</span>
                : <Check width={14} height={14} className="text-emerald-400" />}
            >
              <div className="space-y-1 px-1 pb-2">
                <div className="text-[10px] text-zinc-500">dublado</div>
                <Track edl={edl} side="a" selected={sel?.ep === key ? sel.idx : null}
                  onPick={(idx) => setSel({ ep: key, idx })} />
                <div className="text-[10px] text-zinc-500">original</div>
                <Track edl={edl} side="b" selected={sel?.ep === key ? sel.idx : null}
                  onPick={(idx) => setSel({ ep: key, idx })} />

                {/* listinha dos trechos que pedem decisão */}
                <ol className="mt-1 divide-y divide-zinc-800/60 rounded-lg border border-zinc-800 text-xs">
                  {decidable(key).map((i, pos) => {
                    const s = edl.segments[i]
                    const act = actions[key]?.[i] ?? s.action
                    const active = sel?.ep === key && sel.idx === i
                    return (
                      <li key={i}>
                        <button
                          onClick={() => setSel({ ep: key, idx: i })}
                          className={`flex w-full items-center gap-2 px-2 py-1 text-left hover:bg-zinc-800/60 ${
                            active ? 'bg-zinc-800/80' : ''}`}
                        >
                          <span className="w-5 shrink-0 text-zinc-500 tabular-nums">{pos + 1}.</span>
                          <span className={`h-2 w-2 shrink-0 rounded-full ${SEG_COLOR[s.kind] ?? 'bg-zinc-600'}`} />
                          <span className="min-w-0 flex-1 truncate text-zinc-300">
                            {KIND_LABEL[s.kind]}
                            <span className="ml-1.5 text-zinc-500 tabular-nums">
                              {fmtT(s.a_start)}–{fmtT(s.a_end)}
                            </span>
                          </span>
                          {act ? (
                            <span className="shrink-0 rounded bg-emerald-950 px-1.5 py-px text-[10px] text-emerald-300">
                              {ACTIONS_FOR[s.kind]?.find((a) => a.value === act)?.label ?? act}
                            </span>
                          ) : s.kind === 'replaced' ? (
                            <span className="shrink-0 text-[10px] text-red-400">sem decisão</span>
                          ) : (
                            <span className="shrink-0 text-[10px] text-zinc-500">padrão</span>
                          )}
                        </button>
                      </li>
                    )
                  })}
                </ol>

                {sel?.ep === key && (() => {
                  const seg = edl.segments[sel.idx]
                  const chosen = actions[key]?.[sel.idx] ?? seg.action
                  const prev = step(key, sel.idx, -1)
                  const nxt = step(key, sel.idx, 1)
                  const pos = decidable(key).indexOf(sel.idx) + 1
                  return (
                    <div className="mt-2 rounded-lg border border-zinc-800 bg-zinc-900/50 p-2.5">
                      <div className="flex items-center gap-2">
                        <button
                          onClick={() => prev !== null && setSel({ ep: key, idx: prev })}
                          disabled={prev === null}
                          title="Trecho anterior"
                          className="rounded border border-zinc-700 px-2 py-0.5 text-xs text-zinc-300 hover:bg-zinc-800 disabled:opacity-40"
                        >
                          ‹ anterior
                        </button>
                        <span className="text-xs text-zinc-500 tabular-nums">
                          {pos}/{decidable(key).length}
                        </span>
                        <button
                          onClick={() => nxt !== null && setSel({ ep: key, idx: nxt })}
                          disabled={nxt === null}
                          title="Próximo trecho"
                          className="rounded border border-zinc-700 px-2 py-0.5 text-xs text-zinc-300 hover:bg-zinc-800 disabled:opacity-40"
                        >
                          próximo ›
                        </button>
                        <div className="min-w-0 flex-1 text-sm font-medium text-zinc-200">
                          {KIND_LABEL[seg.kind]}
                          <span className="ml-2 text-xs font-normal text-zinc-500 tabular-nums">
                            dub {fmtT(seg.a_start)}–{fmtT(seg.a_end)}
                            {seg.b_start != null && <> · orig {fmtT(seg.b_start)}–{fmtT(seg.b_end ?? seg.b_start)}</>}
                            {seg.kind === 'replaced'
                              ? <span title="Distância média dos fingerprints ao longo do MIOLO do trecho: 0 = idênticos, 64 = totalmente diferentes; abaixo de 12 é o mesmo conteúdo (provável falso positivo). 64 exato = sem medição (EDL antiga).">
                                  {' '}· miolo: {seg.residual >= 64
                                    ? 'sem medição'
                                    : `${Math.round(seg.residual)}/64 (${seg.residual < 12 ? 'igual' : seg.residual < 20 ? 'parecido' : 'diferente'})`}
                                </span>
                              : <> · confiança {(seg.confidence * 100).toFixed(0)}%</>}
                          </span>
                        </div>
                      </div>
                      <FramePreview jobId={job.id} episode={key} seg={seg} />
                      <div className="mt-2 flex flex-wrap gap-3">
                        {(ACTIONS_FOR[seg.kind] ?? []).map((a) => (
                          <label key={a.value} className="flex items-center gap-1.5 text-sm text-zinc-300">
                            <input
                              type="radio"
                              name={`act-${key}-${sel.idx}`}
                              checked={chosen === a.value}
                              onChange={() => decide(key, sel.idx, a.value)}
                            />
                            {a.label}
                          </label>
                        ))}
                      </div>
                    </div>
                  )
                })()}

                <label className="mt-1 flex items-center gap-1.5 text-xs text-zinc-400">
                  <input
                    type="checkbox"
                    checked={skip.has(key)}
                    onChange={(e) => setSkip((cur) => {
                      const next = new Set(cur)
                      if (e.target.checked) next.add(key)
                      else next.delete(key)
                      return next
                    })}
                  />
                  Pular este episódio (fica como falha; dá para tentar de novo depois)
                </label>
              </div>
            </Collapsible>
          )
        })}
      </div>

      <button
        onClick={submit}
        disabled={busy}
        className="mt-3 inline-flex items-center gap-1.5 rounded-lg bg-amber-600 px-4 py-2 text-sm font-semibold text-zinc-950 hover:bg-amber-500 disabled:opacity-50"
      >
        Aplicar decisões e continuar
      </button>
    </section>
  )
}
