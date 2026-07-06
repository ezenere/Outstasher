import { useCallback, useEffect, useState } from 'react'
import { BrowserRouter, NavLink, Navigate, Route, Routes } from 'react-router-dom'
import { Bookmark, Download, LogOut, MediaVideo, Settings as SettingsIcon } from 'iconoir-react'
import { authStatus, getToken, logout } from './api'
import Movies from './pages/Movies'
import Jobs from './pages/Jobs'
import JobDetail from './pages/JobDetail'
import Settings from './pages/Settings'
import Catalog from './pages/Catalog'
import CatalogItem from './pages/CatalogItem'
import Login from './pages/Login'

function Tab({ to, children }: { to: string; children: React.ReactNode }) {
  return (
    <NavLink
      to={to}
      end={to === '/'}
      className={({ isActive }) =>
        `flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-sm transition-colors ${
          isActive ? 'bg-zinc-800 text-zinc-100' : 'text-zinc-400 hover:text-zinc-200'
        }`
      }
    >
      {children}
    </NavLink>
  )
}

type Gate = 'loading' | 'setup' | 'login' | 'ok'

export default function App() {
  const [gate, setGate] = useState<Gate>('loading')

  const check = useCallback(async () => {
    try {
      const s = await authStatus()
      if (!s.password_set) setGate('setup')
      else if (s.authenticated && getToken()) setGate('ok')
      else setGate('login')
    } catch {
      // API fora do ar: trata como precisa logar
      setGate('login')
    }
  }, [])

  useEffect(() => {
    void check()
    const onExpired = () => setGate('login')
    window.addEventListener('auth-expired', onExpired)
    return () => window.removeEventListener('auth-expired', onExpired)
  }, [check])

  async function doLogout() {
    await logout()
    setGate('login')
  }

  if (gate === 'loading') {
    return <div className="flex min-h-screen items-center justify-center text-zinc-500">Carregando...</div>
  }
  if (gate === 'setup' || gate === 'login') {
    return <Login needsSetup={gate === 'setup'} onDone={() => setGate('ok')} />
  }

  return (
    <BrowserRouter>
      <div className="min-h-screen">
        <header className="sticky top-0 z-10 border-b border-zinc-800 bg-zinc-950/90 backdrop-blur">
          <div className="mx-auto flex h-14 max-w-5xl items-center gap-6 px-4">
            <span className="font-semibold">🎬 Downloader &amp; Merger</span>
            <nav className="flex flex-1 gap-1">
              <Tab to="/">
                <MediaVideo width={16} height={16} /> Filmes
              </Tab>
              <Tab to="/jobs">
                <Download width={16} height={16} /> Downloads
              </Tab>
              <Tab to="/catalog">
                <Bookmark width={16} height={16} /> Catálogo
              </Tab>
              <Tab to="/settings">
                <SettingsIcon width={16} height={16} /> Configurações
              </Tab>
            </nav>
            <button
              onClick={doLogout}
              title="Sair"
              className="flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-sm text-zinc-400 transition-colors hover:text-zinc-200"
            >
              <LogOut width={16} height={16} /> Sair
            </button>
          </div>
        </header>
        <main className="mx-auto max-w-5xl px-4 py-6 pb-28">
          <Routes>
            <Route path="/" element={<Movies />} />
            <Route path="/jobs" element={<Jobs />} />
            <Route path="/jobs/:id" element={<JobDetail />} />
            <Route path="/settings" element={<Settings />} />
            <Route path="/destinations" element={<Navigate to="/settings" replace />} />
            <Route path="/catalog" element={<Catalog />} />
            <Route path="/catalog/item" element={<CatalogItem />} />
          </Routes>
        </main>
      </div>
    </BrowserRouter>
  )
}
