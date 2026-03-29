import { BrowserRouter, NavLink, Route, Routes } from 'react-router-dom'
import { StatusPage } from './pages/Status'
import { StreamPage } from './pages/Stream'
import { SessionsPage } from './pages/Sessions'
import { LogsPage } from './pages/Logs'

const NAV = [
  { path: '/',         label: 'Status',      icon: '◉' },
  { path: '/stream',   label: 'Live Stream', icon: '▶' },
  { path: '/sessions', label: 'Sessions',    icon: '⊞' },
  { path: '/logs',     label: 'Logs',        icon: '≡' },
]

export default function App() {
  return (
    <BrowserRouter>
      <div className="flex h-screen bg-zinc-950 text-zinc-100 overflow-hidden">
        {/* Sidebar */}
        <aside className="w-52 shrink-0 flex flex-col bg-zinc-900 border-r border-zinc-800">
          <div className="px-4 py-5 border-b border-zinc-800">
            <span className="text-sm font-mono font-semibold text-zinc-200 tracking-wider">
              DUCTOR
            </span>
            <span className="ml-2 text-xs text-green-500 font-mono">● live</span>
          </div>
          <nav className="flex-1 px-2 py-3 space-y-1">
            {NAV.map(({ path, label, icon }) => (
              <NavLink
                key={path}
                to={path}
                end={path === '/'}
                className={({ isActive }) =>
                  `flex items-center gap-3 px-3 py-2 rounded text-sm font-mono transition-colors ${
                    isActive
                      ? 'bg-zinc-800 text-green-400 border border-zinc-700'
                      : 'text-zinc-400 hover:text-zinc-200 hover:bg-zinc-800/50'
                  }`
                }
              >
                <span className="w-4 text-center opacity-70">{icon}</span>
                {label}
              </NavLink>
            ))}
          </nav>
          <div className="px-4 py-3 border-t border-zinc-800 text-xs text-zinc-600 font-mono">
            ductor-dashboard
          </div>
        </aside>

        {/* Main content */}
        <main className="flex-1 min-w-0 overflow-auto">
          <Routes>
            <Route path="/"         element={<StatusPage />} />
            <Route path="/stream"   element={<StreamPage />} />
            <Route path="/sessions" element={<SessionsPage />} />
            <Route path="/logs"     element={<LogsPage />} />
          </Routes>
        </main>
      </div>
    </BrowserRouter>
  )
}
