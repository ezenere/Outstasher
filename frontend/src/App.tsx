import { BrowserRouter, NavLink, Navigate, Route, Routes } from 'react-router-dom'
import { Bookmark, Download, MediaVideo, Settings as SettingsIcon } from 'iconoir-react'
import Movies from './pages/Movies'
import Jobs from './pages/Jobs'
import JobDetail from './pages/JobDetail'
import Settings from './pages/Settings'
import Catalog from './pages/Catalog'
import CatalogItem from './pages/CatalogItem'

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

export default function App() {
  return (
    <BrowserRouter>
      <div className="min-h-screen">
        <header className="sticky top-0 z-10 border-b border-zinc-800 bg-zinc-950/90 backdrop-blur">
          <div className="mx-auto flex h-14 max-w-5xl items-center gap-6 px-4">
            <span className="font-semibold">🎬 Downloader &amp; Merger</span>
            <nav className="flex gap-1">
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
