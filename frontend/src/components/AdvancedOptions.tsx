import { useEffect, useState } from 'react'
import { api, type Capabilities, type ConvertOptions } from '../api'

/** Bloco "Opções avançadas" de conversão, compartilhado pelo modal de download
 *  (página de filmes) e pelo modal de conversão manual.
 *
 *  - value === null -> desabilitado (pipeline clássico, tudo em stream copy).
 *  - As opções de codec vêm de /api/capabilities: só o que o ffmpeg do
 *    servidor sabe encodar aparece habilitado.
 *  - A validação final (bitrate da fonte, resolução não-exata etc.) é feita
 *    no servidor na hora da conversão — aqui só damos os controles e as dicas.
 */

interface Props {
  value: ConvertOptions | null
  onChange: (v: ConvertOptions | null) => void
  /** Motivo para bloquear tudo (ex.: "Apenas baixar" marcado). */
  blocked?: string | null
}

export const CONVERT_DEFAULTS: ConvertOptions = {
  video_codec: 'keep',
  hw_accel: 'none',
  preset: 'default',
  resolution: 'keep',
  quality_mode: 'bitrate',
  video_bitrate: 3500,
  crf: null,
  bit_depth: 'keep',
  audio_tracks: 'all',
  audio_codec: 'keep',
  audio_bitrate: null,
  channels: 'keep',
  subtitles: 'default',
}

// bitrate recomendado (kbps) por codec x resolução — vira o default do slider
const REC_VIDEO_KBPS: Record<string, Record<string, number>> = {
  h264: { '4320': 40000, '2160': 16000, '1080': 6000, '720': 3000, '480': 1500, keep: 6000 },
  hevc: { '4320': 25000, '2160': 10000, '1080': 3500, '720': 1800, '480': 900, keep: 3500 },
  av1: { '4320': 20000, '2160': 8000, '1080': 3000, '720': 1500, '480': 750, keep: 3000 },
  vvc: { '4320': 18000, '2160': 7000, '1080': 2600, '720': 1300, '480': 650, keep: 2600 },
  keep: { '4320': 25000, '2160': 10000, '1080': 3500, '720': 1800, '480': 900, keep: 3500 },
}

const PRESET_LABEL: Record<string, string> = {
  veryfast: 'Muito rápido (pior compressão)',
  fast: 'Rápido',
  default: 'Padrão',
  slow: 'Lento (melhor compressão)',
  veryslow: 'Muito lento (máxima compressão)',
}

const RESOLUTION_LABEL: Record<string, string> = {
  keep: 'Manter original',
  '4320': '8K (4320p)',
  '2160': '4K (2160p)',
  '1080': 'Full HD (1080p)',
  '720': 'HD (720p)',
  '480': 'SD (480p)',
}

const AUDIO_HINT: Record<string, string> = {
  ac3: 'Vai até 5.1 — faixas 7.1 sofrem redução de canais.',
  aac: 'Estéreo apenas — multicanal sofre downmix (o encoder nativo embaralha layouts surround).',
  flac: 'Lossless, sem bitrate. Faixas lossy são mantidas (converter só aumentaria o tamanho).',
  opus: 'Suporta até 7.1.',
  vorbis: 'Suporta até 7.1.',
}

// slider logarítmico: posição 0..1000 <-> kbps na faixa [min, max]
function posToKbps(pos: number, min: number, max: number): number {
  const k = min * Math.pow(max / min, pos / 1000)
  if (k >= 20000) return Math.round(k / 1000) * 1000
  if (k >= 2000) return Math.round(k / 100) * 100
  if (k >= 500) return Math.round(k / 50) * 50
  return Math.max(min, Math.round(k / 10) * 10)
}
const kbpsToPos = (k: number, min: number, max: number) =>
  Math.round((1000 * Math.log(k / min)) / Math.log(max / min))

const fmtKbps = (k: number) =>
  k >= 1000 ? `${(k / 1000).toFixed(k >= 10000 ? 0 : 1)} Mbps` : `${k} kbps`

// capabilities não mudam com o servidor de pé: 1 fetch por sessão da página
let capsCache: Promise<Capabilities> | null = null
const getCaps = () => (capsCache ??= api<Capabilities>('/api/capabilities').catch((e) => {
  capsCache = null
  throw e
}))

export default function AdvancedOptions({ value, onChange, blocked }: Props) {
  const [open, setOpen] = useState(false)
  const [caps, setCaps] = useState<Capabilities | null>(null)
  const [capsError, setCapsError] = useState<string | null>(null)
  const [opts, setOpts] = useState<ConvertOptions>(value ?? CONVERT_DEFAULTS)
  const enabled = value !== null

  useEffect(() => {
    getCaps().then(setCaps).catch((e) => setCapsError((e as Error).message))
  }, [])

  function update(patch: Partial<ConvertOptions>) {
    const next = { ...opts, ...patch }
    setOpts(next)
    if (enabled) onChange(next)
  }

  // trocar codec/encoder/resolução re-sugere o bitrate (e o CRF default,
  // cuja escala muda entre software e hardware)
  function updateVideo(patch: Partial<ConvertOptions>) {
    const next = { ...opts, ...patch }
    const cap = caps?.video_codecs.find((c) => c.id === next.video_codec)
    if (next.video_codec !== 'keep' && next.hw_accel !== 'none'
        && !cap?.hw.some((h) => h.id === next.hw_accel)) {
      next.hw_accel = 'none' // o codec escolhido não tem esse encoder de HW
    }
    const hw = cap?.hw.find((h) => h.id === next.hw_accel)
    if (next.bit_depth === '10' && hw && !hw.ten_bit) next.bit_depth = 'keep'
    const rec = REC_VIDEO_KBPS[next.video_codec] ?? REC_VIDEO_KBPS.keep
    next.video_bitrate = rec[next.resolution] ?? rec.keep
    if (next.quality_mode === 'crf') {
      next.crf = hw?.crf.default ?? cap?.crf.default ?? 24
    }
    setOpts(next)
    if (enabled) onChange(next)
  }

  const [vMin, vMax] = caps?.video_bitrate_kbps ?? [100, 150000]
  const [aMin, aMax] = caps?.audio_bitrate_kbps ?? [32, 1024]
  const codecCap = caps?.video_codecs.find((c) => c.id === opts.video_codec)
  const hwCap = codecCap?.hw.find((h) => h.id === opts.hw_accel)
  const crfCap = hwCap?.crf ?? codecCap?.crf ?? { min: 0, max: 51, default: 24 }
  // 10-bit indisponível: H.264 (software desaconselha, HW não suporta) ou
  // encoder de HW sem saída 10-bit
  const tenBitBlocked = opts.hw_accel !== 'none' && opts.video_codec !== 'keep'
    && hwCap !== undefined && !hwCap.ten_bit
  const audioCap = caps?.audio_codecs.find((c) => c.id === opts.audio_codec)

  // há chance de re-encode de vídeo? (senão bitrate/CRF/preset são inertes)
  const videoActive = opts.video_codec !== 'keep' || opts.resolution !== 'keep'
    || opts.bit_depth !== 'keep'
  const audioBitrateActive = opts.audio_codec !== 'keep' && !audioCap?.lossless

  const selectCls = 'rounded-lg border border-zinc-700 bg-zinc-800 px-2 py-1.5 text-sm'

  return (
    <div className="mt-3 rounded-xl border border-zinc-800">
      <button
        type="button"
        onClick={() => setOpen(!open)}
        className="flex w-full items-center gap-2 px-3 py-2 text-sm font-medium text-zinc-300 hover:text-zinc-100"
      >
        <span className="text-xs">{open ? '▾' : '▸'}</span>
        Opções avançadas
        {enabled && !blocked && (
          <span className="rounded bg-purple-950 px-1.5 py-0.5 text-xs font-medium text-purple-300">
            ativas
          </span>
        )}
      </button>

      {open && (
        <div className="border-t border-zinc-800 p-3">
          {blocked && <p className="mb-2 text-xs text-yellow-300/80">{blocked}</p>}
          {capsError && (
            <p className="mb-2 text-xs text-red-300">
              Não deu para ler as capacidades do servidor: {capsError}
            </p>
          )}

          <label className="flex items-center gap-1.5 text-sm text-zinc-300">
            <input
              type="checkbox"
              checked={enabled && !blocked}
              disabled={!!blocked || !caps}
              onChange={(e) => onChange(e.target.checked ? opts : null)}
            />
            Habilitar opções avançadas
          </label>

          <fieldset
            disabled={!enabled || !!blocked}
            className={`mt-3 ${enabled && !blocked ? '' : 'pointer-events-none opacity-40'}`}
          >
            {/* ---- vídeo ---- */}
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
              <label className="flex flex-col gap-1 text-sm">
                <span className="text-zinc-400">Codec de vídeo</span>
                <select
                  value={opts.video_codec}
                  onChange={(e) => updateVideo({ video_codec: e.target.value })}
                  className={selectCls}
                >
                  <option value="keep">Manter original</option>
                  {caps?.video_codecs.map((c) => (
                    <option key={c.id} value={c.id} disabled={!c.available}>
                      {c.label}{c.available ? '' : ' — indisponível no servidor'}
                    </option>
                  ))}
                </select>
              </label>

              <label className={`flex flex-col gap-1 text-sm ${videoActive ? '' : 'opacity-50'}`}>
                <span className="text-zinc-400">Encoder</span>
                <select
                  value={opts.hw_accel}
                  onChange={(e) => updateVideo({ hw_accel: e.target.value })}
                  className={selectCls}
                >
                  <option value="none">Software (CPU)</option>
                  {caps?.hw_accels.map((a) => {
                    const forCodec = opts.video_codec === 'keep'
                      || codecCap?.hw.some((h) => h.id === a.id)
                    const reason = !a.available ? ' — indisponível no servidor'
                      : !forCodec ? ' — não encoda este codec' : ''
                    return (
                      <option key={a.id} value={a.id} disabled={!a.available || !forCodec}>
                        {a.label}{reason}
                      </option>
                    )
                  })}
                </select>
                {opts.hw_accel !== 'none' && (
                  <span className="text-xs text-zinc-500">
                    GPU: muito mais rápido; compressão um pouco pior que software
                    no mesmo bitrate.
                  </span>
                )}
              </label>

              <label className={`flex flex-col gap-1 text-sm ${videoActive ? '' : 'opacity-50'}`}>
                <span className="text-zinc-400">Preset (velocidade × compressão)</span>
                <select
                  value={opts.preset}
                  onChange={(e) => update({ preset: e.target.value })}
                  className={selectCls}
                >
                  {(caps?.presets ?? Object.keys(PRESET_LABEL)).map((p) => (
                    <option key={p} value={p}>{PRESET_LABEL[p] ?? p}</option>
                  ))}
                </select>
              </label>

              <label className="flex flex-col gap-1 text-sm">
                <span className="text-zinc-400">Resolução (nunca aumenta)</span>
                <select
                  value={opts.resolution}
                  onChange={(e) => updateVideo({ resolution: e.target.value })}
                  className={selectCls}
                >
                  {Object.entries(RESOLUTION_LABEL).map(([v, l]) => (
                    <option key={v} value={v}>{l}</option>
                  ))}
                </select>
              </label>

              <label className="flex flex-col gap-1 text-sm">
                <span className="text-zinc-400">Profundidade de cor</span>
                <select
                  value={opts.bit_depth}
                  onChange={(e) => update({ bit_depth: e.target.value })}
                  className={selectCls}
                >
                  <option value="keep">Manter original</option>
                  <option value="10" disabled={tenBitBlocked}>
                    10-bit (comprime melhor, menos banding)
                    {tenBitBlocked ? ' — este encoder só sai em 8-bit' : ''}
                  </option>
                  <option value="8">8-bit (máxima compatibilidade)</option>
                </select>
              </label>

              <label className={`flex flex-col gap-1 text-sm ${videoActive ? '' : 'opacity-50'}`}>
                <span className="text-zinc-400">Qualidade do vídeo</span>
                <select
                  value={opts.quality_mode}
                  onChange={(e) => {
                    const mode = e.target.value as ConvertOptions['quality_mode']
                    update({ quality_mode: mode, crf: mode === 'crf' ? crfCap.default : null })
                  }}
                  className={selectCls}
                >
                  <option value="bitrate">Bitrate alvo</option>
                  <option value="crf">Qualidade constante (CRF)</option>
                </select>
              </label>

              {opts.quality_mode === 'bitrate' ? (
                <label className={`flex flex-col gap-1 text-sm ${videoActive ? '' : 'opacity-50'}`}>
                  <span className="flex justify-between text-zinc-400">
                    <span>Bitrate de vídeo</span>
                    <span className="font-mono text-zinc-200">{fmtKbps(opts.video_bitrate ?? 3500)}</span>
                  </span>
                  <input
                    type="range"
                    min={0}
                    max={1000}
                    value={kbpsToPos(opts.video_bitrate ?? 3500, vMin, vMax)}
                    onChange={(e) => update({ video_bitrate: posToKbps(Number(e.target.value), vMin, vMax) })}
                  />
                  <span className="text-xs text-zinc-500">
                    Se a fonte entregar menos que isso, o vídeo original é mantido.
                  </span>
                </label>
              ) : (
                <label className={`flex flex-col gap-1 text-sm ${videoActive ? '' : 'opacity-50'}`}>
                  <span className="flex justify-between text-zinc-400">
                    <span>CRF (menor = mais qualidade)</span>
                    <span className="font-mono text-zinc-200">{opts.crf ?? crfCap.default}</span>
                  </span>
                  <input
                    type="range"
                    min={crfCap.min}
                    max={crfCap.max}
                    value={opts.crf ?? crfCap.default}
                    onChange={(e) => update({ crf: Number(e.target.value) })}
                  />
                  <span className="text-xs text-zinc-500">
                    O encoder gasta só o bitrate necessário em cada cena.
                    {opts.hw_accel !== 'none'
                      && ' Na GPU usa lookahead estendido; 20–24 ≈ qualidade de arquivo (Blu-ray sem perda visível).'}
                  </span>
                </label>
              )}
            </div>

            {/* ---- áudio ---- */}
            <div className="mt-3 grid grid-cols-1 gap-3 border-t border-zinc-800 pt-3 sm:grid-cols-2">
              <label className="flex flex-col gap-1 text-sm">
                <span className="text-zinc-400">Áudios</span>
                <select
                  value={opts.audio_tracks}
                  onChange={(e) => update({ audio_tracks: e.target.value })}
                  className={selectCls}
                >
                  <option value="all">Manter todos</option>
                  <option value="target">Apenas original e dublagem (+ desconhecidos)</option>
                </select>
              </label>

              <label className="flex flex-col gap-1 text-sm">
                <span className="text-zinc-400">Canais</span>
                <select
                  value={opts.channels}
                  onChange={(e) => update({ channels: e.target.value })}
                  className={selectCls}
                >
                  <option value="keep">Manter originais</option>
                  <option value="surround51">Máx. 5.1</option>
                  <option value="stereo">Estéreo (2.0)</option>
                </select>
                {opts.channels !== 'keep' && (
                  <span className="text-xs text-zinc-500">Reduzir canais força re-encode da faixa.</span>
                )}
              </label>

              <label className="flex flex-col gap-1 text-sm">
                <span className="text-zinc-400">Codec de áudio</span>
                <select
                  value={opts.audio_codec}
                  onChange={(e) => {
                    const id = e.target.value
                    const cap = caps?.audio_codecs.find((c) => c.id === id)
                    update({
                      audio_codec: id,
                      audio_bitrate: id === 'keep' || cap?.lossless ? null : (cap?.default_kbps ?? 256),
                    })
                  }}
                  className={selectCls}
                >
                  <option value="keep">Tentar manter originais</option>
                  {caps?.audio_codecs.map((c) => (
                    <option key={c.id} value={c.id} disabled={!c.available}>
                      {c.label}{c.available ? '' : ' — indisponível no servidor'}
                    </option>
                  ))}
                </select>
                {AUDIO_HINT[opts.audio_codec] && (
                  <span className="text-xs text-zinc-500">{AUDIO_HINT[opts.audio_codec]}</span>
                )}
              </label>

              <label className={`flex flex-col gap-1 text-sm ${audioBitrateActive ? '' : 'opacity-50'}`}>
                <span className="flex justify-between text-zinc-400">
                  <span>Bitrate de áudio (por faixa)</span>
                  <span className="font-mono text-zinc-200">
                    {audioBitrateActive ? `${opts.audio_bitrate ?? 256} kbps` : '—'}
                  </span>
                </span>
                <input
                  type="range"
                  min={0}
                  max={1000}
                  disabled={!audioBitrateActive}
                  value={kbpsToPos(opts.audio_bitrate ?? 256, aMin, aMax)}
                  onChange={(e) => update({ audio_bitrate: posToKbps(Number(e.target.value), aMin, aMax) })}
                />
                <span className="text-xs text-zinc-500">
                  Faixa com bitrate menor que o pedido é mantida como está.
                </span>
              </label>
            </div>

            {/* ---- legendas ---- */}
            <div className="mt-3 grid grid-cols-1 gap-3 border-t border-zinc-800 pt-3 sm:grid-cols-2">
              <label className="flex flex-col gap-1 text-sm">
                <span className="text-zinc-400">Legendas</span>
                <select
                  value={opts.subtitles}
                  onChange={(e) => update({ subtitles: e.target.value })}
                  className={selectCls}
                >
                  <option value="default">Padrão (só dos idiomas com áudio)</option>
                  <option value="all">Manter todas</option>
                  <option value="none">Nenhuma</option>
                </select>
              </label>
            </div>

            <p className="mt-3 text-xs text-zinc-500">
              A validação acontece no servidor com o arquivo real: nada é convertido
              "para cima" — se a fonte entrega menos que o pedido, o stream original
              é mantido (ou o alvo é rebaixado quando o re-encode é inevitável).
            </p>
          </fieldset>
        </div>
      )}
    </div>
  )
}
