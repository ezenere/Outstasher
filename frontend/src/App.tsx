import { useCallback, useEffect, useState } from 'react'
import { BrowserRouter, NavLink, Navigate, Route, Routes } from 'react-router-dom'
import { Bookmark, Download, LogOut, MediaVideo, Settings as SettingsIcon } from 'iconoir-react'
import { api, authStatus, getToken, logout, type JobSummary } from './api'
import { JobsSummaryContext } from './jobsSummary'
import Movies from './pages/Movies'
import Jobs from './pages/Jobs'
import JobDetail from './pages/JobDetail'
import Settings, {
  DestinationsSection, TorrentTargetsSection, ExtraSearchSection, LanguagesSection, SecuritySection,
} from './pages/Settings'
import Catalog from './pages/Catalog'
import CatalogItem from './pages/CatalogItem'
import Login from './pages/Login'
import ProcessMenu from './components/ProcessMenu'
import { DialogProvider } from './components/Dialog'
import logo from './assets/logo.png'

function Tab({ to, children, dot }: { to: string; children: React.ReactNode; dot?: boolean }) {
  return (
    <NavLink
      to={to}
      end={to === '/'}
      className={({ isActive }) =>
        `relative flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-sm transition-colors ${
          isActive ? 'bg-zinc-800 text-zinc-100' : 'text-zinc-400 hover:text-zinc-200'
        }`
      }
    >
      {children}
      {dot && (
        <span className="absolute -top-0.5 -right-0.5 h-2.5 w-2.5 rounded-full bg-red-500 ring-2 ring-zinc-950" />
      )}
    </NavLink>
  )
}

type Gate = 'loading' | 'setup' | 'login' | 'ok'

export default function App() {
  const [gate, setGate] = useState<Gate>('loading')
  const [pending, setPending] = useState(false)
  // fonte única do summary de processos (cabeçalho + tela de Filmes)
  const [summary, setSummary] = useState<JobSummary[]>([])

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

  // polling do summary de processos a cada 5s (só autenticado). Fonte única
  // consumida pelo dropdown do cabeçalho e pela tela de Filmes (via Context).
  useEffect(() => {
    if (gate !== 'ok') return
    async function load() {
      try {
        setSummary(await api<JobSummary[]>('/api/jobs/summary'))
      } catch {
        /* servidor reiniciando; próximo tick */
      }
    }
    void load()
    const t = setInterval(load, 5000)
    return () => clearInterval(t)
  }, [gate])

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
    <DialogProvider>
    <JobsSummaryContext.Provider value={summary}>
    <BrowserRouter>
      <div className="min-h-screen">
        <header className="sticky top-0 z-10 border-b border-zinc-800 bg-zinc-950/90 backdrop-blur">
          <div className="mx-auto flex h-14 max-w-5xl items-center gap-6 px-4">
            <span className="flex items-center gap-2 font-semibold">
              <img src={logo} alt="" className="h-10 w-10 rounded" />
              Outstasher
            </span>
            <nav className="flex flex-1 gap-1">
              <Tab to="/">
                <MediaVideo width={16} height={16} /> Filmes
              </Tab>
              <Tab to="/jobs" dot={pending}>
                <Download width={16} height={16} /> Downloads
              </Tab>
              <Tab to="/catalog">
                <Bookmark width={16} height={16} /> Catálogo
              </Tab>
              <Tab to="/settings">
                <SettingsIcon width={16} height={16} /> Configurações
              </Tab>
            </nav>
            <ProcessMenu items={summary} onPending={setPending} />
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
            <Route path="/settings" element={<Settings />}>
              <Route index element={<Navigate to="destinations" replace />} />
              <Route path="destinations" element={<DestinationsSection />} />
              <Route path="torrents" element={<TorrentTargetsSection />} />
              <Route path="languages" element={<LanguagesSection />} />
              <Route path="searches" element={<ExtraSearchSection />} />
              <Route path="security" element={<SecuritySection />} />
            </Route>
            <Route path="/destinations" element={<Navigate to="/settings" replace />} />
            <Route path="/catalog" element={<Catalog />} />
            <Route path="/catalog/item" element={<CatalogItem />} />
          </Routes>
        </main>
      </div>
    </BrowserRouter>
    </JobsSummaryContext.Provider>
    </DialogProvider>
  )
}
