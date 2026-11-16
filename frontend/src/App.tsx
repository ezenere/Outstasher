import { useCallback, useEffect, useState } from 'react'
import { BrowserRouter, NavLink, Navigate, Route, Routes } from 'react-router-dom'
import { Bookmark, Cube, LogOut, Search, Settings as SettingsIcon } from 'iconoir-react'
import { api, authStatus, logout, type JobSummary } from './api'
import { JobsSummaryContext } from './jobsSummary'
import Movies from './pages/Movies'
import Jobs from './pages/Jobs'
import JobDetail from './pages/JobDetail'
import Settings, {
  ConvertPresetsSection, DestinationsSection, TorrentTargetsSection, ExtraSearchSection,
  SearchRetrySection,
  LanguagesSection, SecuritySection, AdvancedMergeSection,
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

/** Aba da barra inferior do celular (no desktop as abas ficam no cabeçalho). */
function BottomTab({ to, label, icon: Icon, dot }: {
  to: string; label: string; icon: typeof Search; dot?: boolean
}) {
  return (
    <NavLink
      to={to}
      end={to === '/'}
      className={({ isActive }) =>
        `flex flex-col items-center gap-0.5 py-2 text-[11px] ${
          isActive ? 'text-zinc-100' : 'text-zinc-500'
        }`
      }
    >
      <span className="relative">
        <Icon width={22} height={22} />
        {dot && (
          <span className="absolute -top-0.5 -right-1 h-2.5 w-2.5 rounded-full bg-red-500 ring-2 ring-zinc-950" />
        )}
      </span>
      {label}
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
      else if (s.authenticated) setGate('ok')
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
    try {
      await logout()
    } finally {
      setGate('login')
    }
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
          <div className="mx-auto flex h-14 max-w-5xl items-center gap-6 px-4 mobile:gap-2">
            <span className="flex items-center gap-2 font-semibold mobile:flex-1">
              <img src={logo} alt="" className="h-10 w-10 rounded mobile:h-8 mobile:w-8" />
              Outstasher
            </span>
            <nav className="flex flex-1 gap-1 mobile:hidden">
              <Tab to="/">
                <Search width={16} height={16} /> Buscar
              </Tab>
              <Tab to="/jobs" dot={pending}>
                <Cube width={16} height={16} /> Jobs
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
              className="flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-sm text-zinc-400 transition-colors hover:text-zinc-200 mobile:gap-0 mobile:px-2 mobile:py-2 mobile:text-[0px]"
            >
              <LogOut width={16} height={16} className="mobile:h-5 mobile:w-5" /> Sair
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
              <Route path="presets" element={<ConvertPresetsSection />} />
              <Route path="languages" element={<LanguagesSection />} />
              <Route path="searches" element={<ExtraSearchSection />} />
              <Route path="schedule" element={<SearchRetrySection />} />
              <Route path="merge" element={<AdvancedMergeSection />} />
              <Route path="security" element={<SecuritySection />} />
            </Route>
            <Route path="/destinations" element={<Navigate to="/settings" replace />} />
            <Route path="/catalog" element={<Catalog />} />
            <Route path="/catalog/item" element={<CatalogItem />} />
          </Routes>
        </main>
        <nav
          data-mobile-only
          className="hidden mobile:fixed mobile:inset-x-0 mobile:bottom-0 mobile:z-10 mobile:grid mobile:grid-cols-4 mobile:border-t mobile:border-zinc-800 mobile:bg-zinc-950/95 mobile:pb-[env(safe-area-inset-bottom)] mobile:backdrop-blur"
        >
          <BottomTab to="/" label="Buscar" icon={Search} />
          <BottomTab to="/jobs" label="Jobs" icon={Cube} dot={pending} />
          <BottomTab to="/catalog" label="Catálogo" icon={Bookmark} />
          <BottomTab to="/settings" label="Configurações" icon={SettingsIcon} />
        </nav>
      </div>
    </BrowserRouter>
    </JobsSummaryContext.Provider>
    </DialogProvider>
  )
}
