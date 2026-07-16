import { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react'
import { WarningTriangle, Xmark } from 'iconoir-react'

/** Diálogos modais estilizados no lugar dos window.confirm/alert/prompt.
 *
 *  useDialog() devolve { confirm, alert, prompt } — todos assíncronos:
 *    if (await confirm({ ... })) { ... }
 *    const nome = await prompt({ ... })   // string ou null (cancelou)
 *
 *  Um único modal fica montado no topo da árvore (DialogProvider); as chamadas
 *  enfileiram e resolvem a Promise quando o usuário responde.
 */

type Tone = 'default' | 'danger'

interface ConfirmOpts {
  title?: string
  message: React.ReactNode
  confirmText?: string
  cancelText?: string
  tone?: Tone
}

interface AlertOpts {
  title?: string
  message: React.ReactNode
  confirmText?: string
}

interface PromptOpts {
  title?: string
  message?: React.ReactNode
  defaultValue?: string
  placeholder?: string
  confirmText?: string
  cancelText?: string
}

export interface DialogApi {
  confirm: (opts: ConfirmOpts) => Promise<boolean>
  alert: (opts: AlertOpts) => Promise<void>
  prompt: (opts: PromptOpts) => Promise<string | null>
}

const DialogContext = createContext<DialogApi | null>(null)

export function useDialog(): DialogApi {
  const ctx = useContext(DialogContext)
  if (!ctx) throw new Error('useDialog precisa de <DialogProvider>')
  return ctx
}

// estado interno do diálogo aberto
type Kind = 'confirm' | 'alert' | 'prompt'
interface State {
  kind: Kind
  title?: string
  message?: React.ReactNode
  confirmText: string
  cancelText?: string
  tone: Tone
  defaultValue?: string
  placeholder?: string
  resolve: (v: unknown) => void
}

export function DialogProvider({ children }: { children: React.ReactNode }) {
  const [state, setState] = useState<State | null>(null)
  const [value, setValue] = useState('')
  const inputRef = useRef<HTMLInputElement>(null)

  const confirm = useCallback((o: ConfirmOpts) =>
    new Promise<boolean>((resolve) => setState({
      kind: 'confirm', title: o.title, message: o.message,
      confirmText: o.confirmText ?? 'Confirmar', cancelText: o.cancelText ?? 'Cancelar',
      tone: o.tone ?? 'default', resolve: resolve as (v: unknown) => void,
    })), [])

  const alert = useCallback((o: AlertOpts) =>
    new Promise<void>((resolve) => setState({
      kind: 'alert', title: o.title, message: o.message,
      confirmText: o.confirmText ?? 'OK', tone: 'default',
      resolve: resolve as (v: unknown) => void,
    })), [])

  const prompt = useCallback((o: PromptOpts) =>
    new Promise<string | null>((resolve) => {
      setValue(o.defaultValue ?? '')
      setState({
        kind: 'prompt', title: o.title, message: o.message,
        confirmText: o.confirmText ?? 'Salvar', cancelText: o.cancelText ?? 'Cancelar',
        tone: 'default', defaultValue: o.defaultValue, placeholder: o.placeholder,
        resolve: resolve as (v: unknown) => void,
      })
    }), [])

  // ao abrir um prompt, foca (e seleciona) o input
  useEffect(() => {
    if (state?.kind === 'prompt') {
      const t = setTimeout(() => {
        inputRef.current?.focus()
        inputRef.current?.select()
      }, 0)
      return () => clearTimeout(t)
    }
  }, [state])

  function close(result: boolean | string | null) {
    if (!state) return
    state.resolve(result)
    setState(null)
  }

  // resposta positiva (Enter / botão principal): confirm->true, prompt->texto,
  // alert->fecha (o valor é ignorado pela Promise<void>)
  function submit() {
    if (!state) return
    close(state.kind === 'prompt' ? value : true)
  }
  // resposta negativa (Esc / clicar fora / cancelar): confirm->false,
  // prompt->null, alert->fecha
  function dismiss() {
    if (!state) return
    close(state.kind === 'prompt' ? null : false)
  }

  const api: DialogApi = { confirm, alert, prompt }

  return (
    <DialogContext.Provider value={api}>
      {children}
      {state && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4 backdrop-blur-sm"
          onClick={dismiss}
        >
          <div
            role="dialog"
            aria-modal="true"
            className="w-full max-w-sm rounded-2xl border border-zinc-700 bg-zinc-900 shadow-2xl"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-start gap-3 p-5">
              {state.tone === 'danger' && (
                <span className="mt-0.5 flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-red-950 text-red-400">
                  <WarningTriangle width={18} height={18} />
                </span>
              )}
              <div className="min-w-0 flex-1">
                {state.title && <h2 className="text-base font-semibold text-zinc-100">{state.title}</h2>}
                {state.message != null && (
                  <div className={`text-sm whitespace-pre-wrap text-zinc-300 ${state.title ? 'mt-1' : ''}`}>
                    {state.message}
                  </div>
                )}
                {state.kind === 'prompt' && (
                  <input
                    ref={inputRef}
                    value={value}
                    placeholder={state.placeholder}
                    onChange={(e) => setValue(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter') submit()
                      if (e.key === 'Escape') dismiss()
                    }}
                    className="mt-3 w-full rounded-lg border border-zinc-700 bg-zinc-800 px-3 py-2 text-sm text-zinc-100 outline-none focus:border-blue-500"
                  />
                )}
              </div>
              <button
                onClick={dismiss}
                title="Fechar"
                className="rounded-lg p-1 text-zinc-500 hover:text-zinc-300"
              >
                <Xmark width={16} height={16} />
              </button>
            </div>
            <div className="flex justify-end gap-2 border-t border-zinc-800 px-5 py-3">
              {state.kind !== 'alert' && (
                <button
                  onClick={dismiss}
                  className="rounded-lg border border-zinc-700 px-4 py-2 text-sm font-medium text-zinc-300 hover:bg-zinc-800"
                >
                  {state.cancelText}
                </button>
              )}
              <button
                onClick={submit}
                autoFocus={state.kind !== 'prompt'}
                className={`rounded-lg px-4 py-2 text-sm font-semibold text-white ${
                  state.tone === 'danger'
                    ? 'bg-red-600 hover:bg-red-500'
                    : 'bg-blue-600 hover:bg-blue-500'
                }`}
              >
                {state.confirmText}
              </button>
            </div>
          </div>
        </div>
      )}
    </DialogContext.Provider>
  )
}
