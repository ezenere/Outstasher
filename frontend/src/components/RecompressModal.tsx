import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Play, Xmark } from 'iconoir-react'
import { post, type CatalogFile, type ConvertOptions, type Job, type Movie } from '../api'
import AdvancedOptions from './AdvancedOptions'
import { useScrollLock } from './ui'

interface Props {
  folder: string
  destinationId: number | null
  /** Um arquivo (fluxo clássico) OU vários (lote: temporada/série inteira). */
  file?: CatalogFile
  files?: CatalogFile[]
  /** Rótulo do escopo do lote ("Temporada 01" / "série inteira") para o título. */
  batchLabel?: string
  tmdb: Movie | null
  onClose: () => void
}

/** Recompressão de itens que já estão na coleção: mesmas opções avançadas dos
 *  downloads, aplicadas no(s) arquivo(s) do disco. O original só é trocado
 *  quando o ffmpeg termina — cancelar ou falhar deixa tudo intacto. Com vários
 *  arquivos vira um job em lote (falha por arquivo, regra dos 75%). */
export default function RecompressModal({ folder, destinationId, file, files, batchLabel, tmdb, onClose }: Props) {
  const [advanced, setAdvanced] = useState<ConvertOptions | null>(null)
  const [replace, setReplace] = useState(true)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const navigate = useNavigate()
  useScrollLock()
  const batch = files && files.length > 0

  async function submit() {
    if (!advanced || submitting) return
    setSubmitting(true)
    setError(null)
    try {
      const job = batch
        ? await post<Job>('/api/jobs/recompress-batch', {
            folder,
            rels: files!.map((f) => f.rel),
            destination_id: destinationId,
            tmdb_id: tmdb?.id ?? null,
            convert: advanced,
            replace,
          })
        : await post<Job>('/api/jobs/recompress', {
            folder,
            rel: file!.rel,
            destination_id: destinationId,
            tmdb_id: tmdb?.id ?? null,
            convert: advanced,
            replace,
          })
      navigate(`/jobs/${job.id}`)
    } catch (e) {
      setError((e as Error).message)
      setSubmitting(false)
    }
  }

  const radio = 'flex items-start gap-2 rounded-lg border p-3 text-sm cursor-pointer'

  return (
    <div className="fixed inset-0 z-30 flex justify-center overflow-y-auto bg-black/60 p-4" onClick={onClose}>
      {/* my-auto (e não items-center) centraliza quando cabe, mas deixa o modal
          crescer para baixo quando o conteúdo passa da tela — com items-center o
          topo saía da viewport e ficava inacessível ao rolar */}
      <div
        className="my-auto h-fit w-full max-w-2xl rounded-2xl border border-zinc-700 bg-zinc-900 p-5"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center gap-2">
          <h2 className="flex-1 text-lg font-semibold">
            {batch ? `Recomprimir ${batchLabel ?? `${files!.length} arquivo(s)`}` : 'Recomprimir filme'}
          </h2>
          <button onClick={onClose} className="rounded-lg border border-zinc-700 p-1.5 text-zinc-400 hover:text-zinc-200" title="Fechar">
            <Xmark width={16} height={16} />
          </button>
        </div>

        {batch ? (
          <div className="mt-3 max-h-40 overflow-y-auto rounded-lg border border-zinc-800 bg-zinc-950/40 px-3 py-2">
            <div className="mb-1 text-xs text-zinc-400">
              {files!.length} arquivo(s) — cada um falha/conclui sozinho; menos
              de 75% de sucesso aborta o job
            </div>
            {files!.map((f) => (
              <div key={f.rel} className="truncate font-mono text-xs text-zinc-300">{f.rel}</div>
            ))}
          </div>
        ) : (
          <div className="mt-3 rounded-lg border border-zinc-800 bg-zinc-950/40 px-3 py-2">
            <div className="truncate font-mono text-sm">{file!.name}</div>
            <div className="mt-0.5 text-xs text-zinc-500">
              {file!.size_human}
              {file!.duration ? ` · ${file!.duration}` : ''}
              {file!.overall_bitrate ? ` · ${file!.overall_bitrate}` : ''}
            </div>
          </div>
        )}

        {/* o que fazer com o original */}
        <div className="mt-4 grid grid-cols-1 gap-2 sm:grid-cols-2">
          <label className={`${radio} ${replace ? 'border-blue-700 bg-blue-950/30' : 'border-zinc-700'}`}>
            <input type="radio" checked={replace} onChange={() => setReplace(true)} className="mt-0.5" />
            <span>
              <span className="font-medium">Substituir o original</span>
              <span className="mt-0.5 block text-xs text-zinc-400">
                Troca o arquivo só quando a conversão termina. Recupera espaço.
              </span>
            </span>
          </label>
          <label className={`${radio} ${!replace ? 'border-blue-700 bg-blue-950/30' : 'border-zinc-700'}`}>
            <input type="radio" checked={!replace} onChange={() => setReplace(false)} className="mt-0.5" />
            <span>
              <span className="font-medium">Manter os dois</span>
              <span className="mt-0.5 block text-xs text-zinc-400">
                Grava como “[recomprimido]” ao lado, para você comparar e apagar depois.
              </span>
            </span>
          </label>
        </div>

        <AdvancedOptions value={advanced} onChange={setAdvanced} />
        {!advanced && (
          <p className="mt-2 text-xs text-zinc-500">
            Habilite as opções avançadas e escolha o que mudar — é o que define a recompressão.
          </p>
        )}

        {error && (
          <div className="mt-3 rounded-lg border border-red-900/60 bg-red-950/30 px-3 py-2 text-sm text-red-300">
            {error}
          </div>
        )}

        <div className="mt-4 flex justify-end gap-2">
          <button onClick={onClose} className="rounded-lg border border-zinc-700 px-4 py-2 text-sm text-zinc-300 hover:bg-zinc-800">
            Cancelar
          </button>
          <button
            onClick={submit}
            disabled={!advanced || submitting}
            className="inline-flex items-center gap-1.5 rounded-lg bg-blue-600 px-4 py-2 text-sm font-semibold hover:bg-blue-500 disabled:opacity-50"
          >
            {submitting ? 'Criando...' : <><Play width={15} height={15} /> Recomprimir</>}
          </button>
        </div>
      </div>
    </div>
  )
}
