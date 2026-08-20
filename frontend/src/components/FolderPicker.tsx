import { useCallback, useEffect, useState } from 'react'
import { ArrowUp, Check, Folder, MediaVideo, Xmark } from 'iconoir-react'
import { api, fmtSize, type BrowseDir } from '../api'
import { useScrollLock } from './ui'

interface Props {
  /** 'dir' devolve a pasta aberta; 'file' devolve um arquivo clicado. */
  mode: 'dir' | 'file'
  title: string
  /** Onde abrir (default: o primeiro atalho do servidor). */
  start?: string | null
  onPick: (path: string) => void
  onClose: () => void
}

/** Navegador de pastas do servidor: escolhe um caminho sem digitar.
 *
 *  Genérico de propósito — qualquer tela que precise de um caminho usa este
 *  mesmo popup. Começa nos ATALHOS (destinos cadastrados, pasta dos torrents),
 *  porque é de lá que os arquivos saem; `/` continua a um clique. */
export default function FolderPicker({ mode, title, start, onPick, onClose }: Props) {
  const [data, setData] = useState<BrowseDir | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  useScrollLock()

  const load = useCallback(async (path?: string | null) => {
    setLoading(true)
    setError(null)
    try {
      const q = path ? `?path=${encodeURIComponent(path)}` : ''
      setData(await api<BrowseDir>(`/api/browse${q}`))
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { void load(start) }, [load, start])

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4"
      onClick={onClose}>
      <div className="flex max-h-[85vh] w-full max-w-3xl flex-col overflow-hidden rounded-2xl border border-zinc-800 bg-zinc-900"
        onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center gap-3 border-b border-zinc-800 px-4 py-3">
          <Folder width={18} height={18} className="text-zinc-400" />
          <h2 className="flex-1 text-sm font-semibold">{title}</h2>
          <button onClick={onClose} className="text-zinc-500 hover:text-zinc-300">
            <Xmark width={18} height={18} />
          </button>
        </div>

        {/* atalhos: destinos e pasta dos torrents */}
        {data?.shortcuts?.length ? (
          <div className="flex flex-wrap gap-1.5 border-b border-zinc-800/60 px-4 py-2">
            {data.shortcuts.map((s) => (
              <button key={s.path} onClick={() => void load(s.path)}
                className="rounded-md bg-zinc-800 px-2 py-1 text-xs text-zinc-300 hover:bg-zinc-700">
                {s.label}
              </button>
            ))}
            <button onClick={() => void load('/')}
              className="rounded-md bg-zinc-800 px-2 py-1 text-xs text-zinc-400 hover:bg-zinc-700">
              /
            </button>
          </div>
        ) : null}

        <div className="flex items-center gap-2 border-b border-zinc-800/60 px-4 py-2">
          <button
            onClick={() => data?.parent && void load(data.parent)}
            disabled={!data?.parent}
            title="Pasta acima"
            className="rounded-md border border-zinc-700 p-1 text-zinc-400 hover:bg-zinc-800 disabled:opacity-40"
          >
            <ArrowUp width={14} height={14} />
          </button>
          <code className="min-w-0 flex-1 truncate text-xs text-zinc-400">
            {data?.path ?? '…'}
          </code>
        </div>

        <div className="min-h-0 flex-1 overflow-auto">
          {error && <div className="p-4 text-sm text-red-400">{error}</div>}
          {loading && <div className="p-4 text-sm text-zinc-500">Lendo…</div>}
          {!loading && !error && data && (
            <ul className="divide-y divide-zinc-800/60">
              {data.dirs.map((d) => (
                <li key={d.path}>
                  <button onClick={() => void load(d.path)}
                    className="flex w-full items-center gap-2 px-4 py-2 text-left text-sm hover:bg-zinc-800/50">
                    <Folder width={15} height={15} className="shrink-0 text-blue-400" />
                    <span className="truncate">{d.name}</span>
                  </button>
                </li>
              ))}
              {data.files.map((f) => (
                <li key={f.path}>
                  <button
                    onClick={() => mode === 'file' && onPick(f.path)}
                    disabled={mode !== 'file'}
                    className={`flex w-full items-center gap-2 px-4 py-2 text-left text-sm ${
                      mode === 'file' ? 'hover:bg-zinc-800/50' : 'cursor-default opacity-60'}`}
                  >
                    <MediaVideo width={15} height={15} className="shrink-0 text-zinc-500" />
                    <span className="min-w-0 flex-1 truncate">{f.name}</span>
                    <span className="shrink-0 text-xs text-zinc-500">{fmtSize(f.size)}</span>
                  </button>
                </li>
              ))}
              {!data.dirs.length && !data.files.length && (
                <li className="px-4 py-3 text-sm text-zinc-500">Pasta vazia.</li>
              )}
              {data.truncated && (
                <li className="px-4 py-2 text-xs text-amber-400">
                  Pasta grande demais: mostrando só o começo.
                </li>
              )}
            </ul>
          )}
        </div>

        {mode === 'dir' && (
          <div className="flex items-center justify-end gap-2 border-t border-zinc-800 px-4 py-3">
            <button onClick={onClose}
              className="rounded-lg border border-zinc-700 px-3 py-1.5 text-sm text-zinc-300 hover:bg-zinc-800">
              Cancelar
            </button>
            <button
              onClick={() => data && onPick(data.path)}
              disabled={!data}
              className="inline-flex items-center gap-1.5 rounded-lg bg-blue-600 px-4 py-1.5 text-sm font-semibold hover:bg-blue-500 disabled:opacity-50"
            >
              <Check width={15} height={15} /> Usar esta pasta
            </button>
          </div>
        )}
      </div>
    </div>
  )
}
