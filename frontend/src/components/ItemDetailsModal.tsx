import { useEffect, useState, type ReactNode } from 'react'
import { CheckCircle, WarningTriangle, Xmark } from 'iconoir-react'
import {
  fetchDetails, fetchDetailsProbe, fmtSize, fmtWhen,
  type EpisodeState, type ItemDetails, type MergeReport, type ProbeFile, type ProbeStream,
} from '../api'
import { AlignTimeline } from './AlignTimeline'
import { EpisodeBadge } from './SeriesJobSections'
import { Badge, useScrollLock } from './ui'

/** Ficha do episódio (ou do filme, sem `ep`): arquivos e de onde vieram,
 *  faixas de cada arquivo, como foi processado (método, offsets, timeline do
 *  alinhamento e do render) e o log só dele. */

const FILE_LABEL: Record<string, string> = {
  original: 'Original (vídeo)',
  dubbed: 'Dublado (áudio)',
  file: 'Arquivo baixado',
  output: 'Arquivo final',
}

const VERDICT_LABEL: Record<string, string> = {
  constant: 'offset constante',
  drift: 'drift linear',
  cut: 'degrau (corte/cena diferente)',
  unknown: 'offset não confirmado',
}

function dur(s: number | null | undefined): string {
  if (!s) return '—'
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const sec = Math.floor(s % 60)
  return h ? `${h}h${String(m).padStart(2, '0')}m${String(sec).padStart(2, '0')}s`
    : `${m}m${String(sec).padStart(2, '0')}s`
}

function kbps(b: number | null | undefined): string | null {
  if (!b) return null
  return b >= 1_000_000 ? `${(b / 1_000_000).toFixed(1)} Mb/s` : `${Math.round(b / 1000)} kb/s`
}

function Section({ title, children, right }: { title: string; children: ReactNode; right?: ReactNode }) {
  return (
    <section className="mt-5">
      <div className="mb-2 flex items-center gap-2">
        <h3 className="text-sm font-semibold text-zinc-300">{title}</h3>
        <div className="flex-1" />
        {right}
      </div>
      {children}
    </section>
  )
}

function KV({ k, children }: { k: string; children: ReactNode }) {
  return (
    <div className="flex gap-3 text-sm">
      <div className="w-36 shrink-0 text-zinc-500">{k}</div>
      <div className="min-w-0 flex-1 text-zinc-200">{children}</div>
    </div>
  )
}

function streamLine(s: ProbeStream): string {
  const partes: (string | null | undefined)[] = [s.codec?.toUpperCase(), s.profile]
  if (s.type === 'video') {
    partes.push(s.width && s.height ? `${s.width}×${s.height}` : null,
      s.fps ? `${s.fps} fps` : null, s.pix_fmt, s.hdr ? 'HDR' : null)
  } else if (s.type === 'audio') {
    partes.push(s.layout || (s.channels ? `${s.channels} canais` : null),
      s.sample_rate ? `${s.sample_rate / 1000} kHz` : null)
  }
  partes.push(kbps(s.bit_rate))
  return partes.filter(Boolean).join(' · ')
}

const TYPE_LABEL: Record<string, string> = {
  video: 'Vídeo', audio: 'Áudio', subtitle: 'Legenda', attachment: 'Anexo', data: 'Dados',
}

function Tracks({ f }: { f: ProbeFile }) {
  if (!f.exists) return <div className="text-xs text-zinc-500">Arquivo não existe mais (limpeza dos torrents ou movido).</div>
  if (f.error) return <div className="text-xs text-red-400">{f.error}</div>
  return (
    <div className="overflow-x-auto">
      <table className="w-full border-collapse text-xs">
        <tbody>
          {(f.streams ?? []).map((s) => (
            <tr key={s.index} className="border-t border-zinc-800/60">
              <td className="py-1 pr-2 text-zinc-500 tabular-nums">#{s.index}</td>
              <td className="py-1 pr-2 text-zinc-300">{s.cover ? 'Capa' : (TYPE_LABEL[s.type] ?? s.type)}</td>
              <td className="py-1 pr-2 font-mono text-zinc-400">{s.lang ?? 'und'}</td>
              <td className="py-1 pr-2 text-zinc-200">{streamLine(s)}</td>
              <td className="py-1 pr-2 text-zinc-500">{s.title}</td>
              <td className="py-1 text-right text-zinc-500">
                {s.default && <span className="ml-1 rounded bg-zinc-800 px-1">padrão</span>}
                {s.forced && <span className="ml-1 rounded bg-zinc-800 px-1">forçada</span>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

/** Janelas de offset medidas: cada uma como chip "tempo · offset", as que
 *  fogem da primeira em âmbar (é onde o caminho rápido desistiu). */
function Windows({ pts, title }: { pts: [number, number, number][]; title: string }) {
  if (!pts.length) return null
  const base = pts[0][1]
  return (
    <KV k={title}>
      <div className="flex flex-wrap gap-1.5">
        {pts.map(([t, o, q], i) => {
          const fora = Math.abs(o - base) > 0.05
          return (
            <span key={i} title={`qualidade do pico: ${q}`}
              className={`rounded px-1.5 py-0.5 text-xs tabular-nums ${
                fora ? 'bg-amber-950 text-amber-300' : 'bg-zinc-800 text-zinc-300'}`}>
              {Math.round(t / 60)}min {o >= 0 ? '+' : ''}{Math.round(o * 1000)}ms
            </span>
          )
        })}
      </div>
    </KV>
  )
}

function Processing({ d, r }: { d: ItemDetails; r: MergeReport | null }) {
  if (!r && !d.edl) {
    return <div className="text-sm text-zinc-500">Ainda não processado.</div>
  }
  return (
    <div className="space-y-1.5">
      {r?.method_label && <KV k="Método">{r.method_label}</KV>}
      {r?.offset_ms != null && <KV k="Offset aplicado">{r.offset_ms >= 0 ? '+' : ''}{r.offset_ms.toFixed(1)} ms</KV>}
      {r?.verdict && <KV k="Veredito das janelas">{VERDICT_LABEL[r.verdict] ?? r.verdict}</KV>}
      {r?.mode && <KV k="Como">{r.mode}</KV>}
      {r?.scan && (
        <Windows title={r.scan.constant ? 'Varredura (constante)' : 'Varredura (variou)'}
          pts={r.scan.points} />
      )}
      {r?.windows && <Windows title="Janelas do merge" pts={r.windows} />}
      {r?.durations && (
        <KV k="Durações">dublado {dur(r.durations.dub)} · original {dur(r.durations.orig)}</KV>
      )}
      {r?.subs_muxed ? <KV k="Legendas externas">{r.subs_muxed} no mux final</KV> : null}
      {(r?.started_at || r?.finished_at) && (
        <KV k="Quando">
          {r.started_at
            ? <>{fmtWhen(r.started_at)} → {r.finished_at ? fmtWhen(r.finished_at) : 'em andamento'}</>
            : <>concluído {fmtWhen(r.finished_at)}</>}
        </KV>
      )}
      {d.edl?.note && <KV k="Arquivo fundido">{d.edl.note}</KV>}
      {r?.notes?.map((n, i) => (
        <div key={i} className="text-xs text-amber-300">{n}</div>
      ))}
    </div>
  )
}

export default function ItemDetailsModal({ jobId, ep, onClose }: {
  jobId: string
  /** episódio (S01E02); null/undefined = ficha do filme */
  ep?: string | null
  onClose: () => void
}) {
  const [d, setD] = useState<ItemDetails | null>(null)
  const [probe, setProbe] = useState<Record<string, ProbeFile> | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [probeErr, setProbeErr] = useState<string | null>(null)
  useScrollLock()  // só o conteúdo da ficha rola

  useEffect(() => {
    let dead = false
    setD(null); setProbe(null); setErr(null); setProbeErr(null)
    fetchDetails(jobId, ep).then((x) => { if (!dead) setD(x) })
      .catch((e: Error) => { if (!dead) setErr(e.message) })
    // faixas à parte: ffprobe de arquivo grande em disco lento demora
    fetchDetailsProbe(jobId, ep).then((x) => { if (!dead) setProbe(x) })
      .catch((e: Error) => { if (!dead) setProbeErr(e.message) })
    return () => { dead = true }
  }, [jobId, ep])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  const files = d ? Object.entries(d.files).filter(([, p]) => p) as [string, string][] : []
  const r = d?.report ?? null

  return (
    <div className="fixed inset-0 z-30 flex justify-center overflow-y-auto bg-black/60 p-4" onClick={onClose}>
      <div className="my-auto h-fit w-full max-w-4xl rounded-2xl border border-zinc-700 bg-zinc-900 p-5"
        onClick={(e) => e.stopPropagation()}>
        <div className="flex items-start gap-3">
          <div className="min-w-0 flex-1">
            <h2 className="text-lg font-semibold">
              {ep ? <><span className="font-mono">{ep}</span>{d?.name ? ` — ${d.name}` : ''}</> : 'Detalhes da conversão'}
            </h2>
            {d && (
              <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-zinc-500">
                {ep ? <EpisodeBadge state={d.state as EpisodeState} /> : d.state && <Badge status={d.state} />}
                {d.air_date && <span>estreia {d.air_date}</span>}
                {d.runtime ? <span>· {d.runtime} min</span> : null}
              </div>
            )}
          </div>
          <button onClick={onClose} className="rounded-lg border border-zinc-700 p-1.5 text-zinc-400 hover:text-zinc-200" title="Fechar">
            <Xmark width={16} height={16} />
          </button>
        </div>

        {err && <div className="mt-4 text-sm text-red-400">{err}</div>}
        {!d && !err && <div className="mt-4 text-sm text-zinc-500">Carregando…</div>}

        {d && (
          <>
            {d.error && (
              <div className="mt-4 flex gap-2 rounded-lg border border-red-900 bg-red-950/40 p-3 text-sm text-red-300">
                <WarningTriangle width={16} height={16} className="mt-0.5 shrink-0" />
                <span className="break-words">{d.error}</span>
              </div>
            )}

            <Section title="Arquivos">
              <div className="space-y-2">
                {files.map(([role, path]) => {
                  const pf = probe?.[role]
                  return (
                    <div key={role} className="rounded-lg border border-zinc-800 bg-zinc-950/40 px-3 py-2">
                      <div className="flex flex-wrap items-center gap-2 text-xs">
                        <span className="font-medium text-zinc-300">{FILE_LABEL[role] ?? role}</span>
                        {pf && (pf.exists
                          ? <span className="inline-flex items-center gap-1 text-emerald-400"><CheckCircle width={12} height={12} /> existe</span>
                          : <span className="text-red-400">não existe mais</span>)}
                        {pf?.size != null && <span className="text-zinc-500">{fmtSize(pf.size)}</span>}
                        {pf?.duration ? <span className="text-zinc-500">· {dur(pf.duration)}</span> : null}
                        {pf?.bit_rate ? <span className="text-zinc-500">· {kbps(pf.bit_rate)}</span> : null}
                        {pf?.chapters ? <span className="text-zinc-500">· {pf.chapters} capítulo(s)</span> : null}
                      </div>
                      <div className="mt-0.5 break-all font-mono text-xs text-zinc-400">{path}</div>
                    </div>
                  )
                })}
                {Object.entries(d.subs).filter(([, l]) => l?.length).map(([role, l]) => (
                  <div key={role} className="text-xs text-zinc-400">
                    Legendas externas ({role === 'original' ? 'original' : 'dublado'}):
                    {l.map((p) => <div key={p} className="break-all font-mono text-zinc-500">{p}</div>)}
                  </div>
                ))}
              </div>
            </Section>

            {d.torrents.length > 0 && (
              <Section title="Torrents">
                <div className="space-y-1">
                  {d.torrents.map((t) => (
                    <div key={t.n} className="flex flex-wrap items-center gap-2 text-xs">
                      <span className="rounded bg-zinc-800 px-1.5 py-0.5 text-zinc-300">
                        t{t.n} · {t.role === 'video' || t.role === 'original' ? 'original' : t.role === 'audio' || t.role === 'dubbed' ? 'dublado' : t.role}
                      </span>
                      <span className="min-w-0 flex-1 truncate text-zinc-300" title={t.title}>{t.title}</span>
                      {t.size ? <span className="text-zinc-500">{fmtSize(t.size)}</span> : null}
                    </div>
                  ))}
                </div>
              </Section>
            )}

            <Section title="Processamento">
              <Processing d={d} r={r} />
            </Section>

            {(d.edl || r?.plan) && (
              <Section title="Alinhamento e render">
                <AlignTimeline
                  segments={d.edl?.segments}
                  durDub={d.edl?.source_dub.duration}
                  durOrig={d.edl?.source_orig.duration}
                  plan={r?.plan}
                  window={d.edl?.b_window ?? r?.plan?.window ?? null}
                />
                {!r?.plan && (
                  <div className="mt-1 text-xs text-zinc-500">
                    O plano do render (fatias e cortes) aparece aqui para o que for renderizado daqui em diante.
                  </div>
                )}
              </Section>
            )}

            <Section title="Faixas" right={!probe && !probeErr
              ? <span className="text-xs text-zinc-500">sondando os arquivos…</span> : undefined}>
              {probeErr && <div className="text-xs text-red-400">{probeErr}</div>}
              {probe && (
                <div className="space-y-3">
                  {Object.entries(probe).map(([role, f]) => (
                    <div key={role}>
                      <div className="mb-1 text-xs font-medium text-zinc-400">{FILE_LABEL[role] ?? role}
                        {f.format && <span className="ml-2 font-normal text-zinc-500">{f.format}</span>}
                      </div>
                      <Tracks f={f} />
                    </div>
                  ))}
                </div>
              )}
            </Section>

            {ep && (
              <Section title={`Log do episódio (${d.events.length})`}>
                {d.events.length === 0 ? (
                  <div className="text-sm text-zinc-500">Nada registrado para este episódio.</div>
                ) : (
                  <div className="max-h-80 overflow-y-auto rounded-lg border border-zinc-800 bg-zinc-950/60 p-2 font-mono text-xs">
                    {d.events.map((e, i) => (
                      <div key={i} className="flex gap-2 py-0.5">
                        <span className="shrink-0 text-zinc-600 tabular-nums">{e.ts.slice(11, 19)}</span>
                        <span className="whitespace-pre-wrap break-words text-zinc-300">{e.message}</span>
                      </div>
                    ))}
                  </div>
                )}
              </Section>
            )}
          </>
        )}
      </div>
    </div>
  )
}
