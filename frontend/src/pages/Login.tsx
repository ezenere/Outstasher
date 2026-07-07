import { useState } from 'react'
import { login, setupPassword } from '../api'
import logo from '../assets/logo.png'

/** Tela de senha: modo "setup" cria a primeira senha, "login" autentica. */
export default function Login({ needsSetup, onDone }: {
  needsSetup: boolean
  onDone: () => void
}) {
  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  async function submit(e: React.FormEvent) {
    e.preventDefault()
    setError(null)
    if (needsSetup && password !== confirm) {
      setError('As senhas não conferem.')
      return
    }
    setBusy(true)
    try {
      if (needsSetup) await setupPassword(password)
      else await login(password)
      onDone()
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center px-4">
      <form
        onSubmit={submit}
        className="w-full max-w-sm rounded-2xl border border-zinc-800 bg-zinc-900 p-6 shadow-xl"
      >
        <div className="mb-5 flex flex-col items-center gap-2 text-center">
          <img src={logo} alt="Outstasher" className="h-48 w-48 rounded-xl" />
          <h1 className="text-lg font-semibold">Outstasher</h1>
          <p className="text-sm text-zinc-400">
            {needsSetup
              ? 'Primeira vez aqui — crie uma senha para proteger o acesso.'
              : 'Digite a senha para continuar.'}
          </p>
        </div>

        <label className="block text-sm">
          <span className="text-zinc-400">Senha</span>
          <input
            type="password"
            autoFocus
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            className="mt-1 w-full rounded-lg border border-zinc-700 bg-zinc-800 px-3 py-2 text-sm outline-none focus:border-blue-500"
          />
        </label>

        {needsSetup && (
          <label className="mt-3 block text-sm">
            <span className="text-zinc-400">Confirmar senha</span>
            <input
              type="password"
              value={confirm}
              onChange={(e) => setConfirm(e.target.value)}
              className="mt-1 w-full rounded-lg border border-zinc-700 bg-zinc-800 px-3 py-2 text-sm outline-none focus:border-blue-500"
            />
          </label>
        )}

        {error && <div className="mt-3 text-sm text-red-400">{error}</div>}

        <button
          type="submit"
          disabled={busy || !password || (needsSetup && !confirm)}
          className="mt-5 w-full rounded-lg bg-blue-600 px-4 py-2.5 text-sm font-semibold hover:bg-blue-500 disabled:opacity-50"
        >
          {busy ? '...' : needsSetup ? 'Criar senha e entrar' : 'Entrar'}
        </button>
      </form>
    </div>
  )
}
