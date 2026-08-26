import { useEffect, useState } from 'react'
import { api, type AdvancedMergeConfig, type AdvancedMergeInfo } from '../api'
import AdvancedOptions, { CONVERT_DEFAULTS } from './AdvancedOptions'

interface Props {
  /** null = usar a configuração global (Configurações → Merge avançado) */
  value: AdvancedMergeConfig | null
  onChange: (v: AdvancedMergeConfig | null) => void
  /** sem a opção "usar a global" (a própria tela de Configurações) */
  forceCustom?: boolean
  title?: string
}

const field = 'rounded-lg border border-zinc-700 bg-zinc-800 px-2 py-1.5 text-sm outline-none focus:border-blue-500'

/** Política do merge avançado: o que fazer com trecho sem dublagem e como
 *  re-encodar nos cortes. Um só painel para Configurações (global), para o
 *  modal de download ("se precisar do merge avançado") e para o botão de
 *  merge avançado do filme — todos herdam a global por padrão. */
export default function AdvancedMergePolicy({ value, onChange, forceCustom, title }: Props) {
  const [global, setGlobal] = useState<AdvancedMergeConfig | null>(null)
  useEffect(() => {
    api<AdvancedMergeInfo>('/api/advanced-merge').then((i) => setGlobal(i.config)).catch(() => {})
  }, [])
  const custom = value !== null
  const cfg = value ?? global

  return (
    <div className="rounded-xl border border-zinc-800 p-3">
      {title && <div className="mb-2 text-sm font-semibold text-zinc-300">{title}</div>}
      {!forceCustom && (
        <label className="mb-2 flex items-center gap-2 text-sm text-zinc-300">
          <input type="checkbox" checked={!custom}
            onChange={(e) => onChange(e.target.checked ? null : (global ?? { undubbed: 'cut', cut_min_s: 1, reencode: null }))} />
          Usar a configuração global (Configurações → Merge avançado)
        </label>
      )}
      {cfg && (custom || forceCustom) && (
        <div className="flex flex-col gap-4">
          <section>
            <div className="mb-1 text-xs font-semibold uppercase tracking-wide text-zinc-500">Trecho do vídeo sem dublagem</div>
            <div className="flex flex-col gap-1.5 text-sm text-zinc-300">
              <label className="flex items-start gap-2">
                <input type="radio" checked={cfg.undubbed === 'cut'} className="mt-1"
                  onChange={() => onChange({ ...cfg, undubbed: 'cut' })} />
                <span><b>A cena sai do vídeo</b> — só fica o que existe nos dois lados.
                  <span className="block text-xs text-zinc-500">Trechos menores que o limite ficam mudos em vez de cortados.</span></span>
              </label>
              {cfg.undubbed === 'cut' && (
                <label className="ml-6 flex items-center gap-2 text-xs text-zinc-400">
                  Cortar a partir de
                  <input type="number" min={0} step={0.5} value={cfg.cut_min_s}
                    onChange={(e) => onChange({ ...cfg, cut_min_s: Number(e.target.value) })}
                    className={`${field} w-20 text-right`} /> s
                </label>
              )}
              <label className="flex items-start gap-2">
                <input type="radio" checked={cfg.undubbed === 'silence'} className="mt-1"
                  onChange={() => onChange({ ...cfg, undubbed: 'silence' })} />
                <span><b>Fica mudo</b> — o vídeo continua, sem áudio no trecho.</span>
              </label>
              <label className="flex items-start gap-2">
                <input type="radio" checked={cfg.undubbed === 'fill'} className="mt-1"
                  onChange={() => onChange({ ...cfg, undubbed: 'fill' })} />
                <span><b>Recebe o áudio original</b> — no idioma original da obra.</span>
              </label>
            </div>
          </section>
          <section>
            <div className="mb-1 text-xs font-semibold uppercase tracking-wide text-zinc-500">Re-encode nos cortes</div>
            <p className="mb-1.5 text-xs text-zinc-500">
              Corte por cópia cai em keyframe (até ~2 s de raspa). Re-encodar com keyframe forçado
              em cada corte deixa o corte exato no frame. Só as opções de <b>vídeo</b> valem aqui.
            </p>
            <label className="mb-2 flex items-center gap-2 text-sm text-zinc-300">
              <input type="checkbox" checked={cfg.reencode !== null}
                onChange={(e) => onChange({ ...cfg, reencode: e.target.checked
                  ? { ...CONVERT_DEFAULTS, video_codec: 'av1', quality_mode: 'crf', crf: 20 } : null })} />
              Recodificar o vídeo nos cortes
            </label>
            {cfg.reencode !== null && (
              <AdvancedOptions value={cfg.reencode} onChange={(v) => onChange({ ...cfg, reencode: v })}
                hidePresets hideTitle hideButtton />
            )}
          </section>
        </div>
      )}
      {!custom && !forceCustom && global && (
        <div className="text-xs text-zinc-500">
          Global agora: {global.undubbed === 'cut' ? `cena sai do vídeo (a partir de ${global.cut_min_s} s)`
            : global.undubbed === 'silence' ? 'trecho mudo' : 'áudio original'};
          {' '}{global.reencode ? `re-encode ${global.reencode.video_codec.toUpperCase()}${global.reencode.hw_accel !== 'none' ? ' (' + global.reencode.hw_accel.toUpperCase() + ')' : ''}` : 'sem re-encode'}.
        </div>
      )}
    </div>
  )
}
