import { useEffect, useState } from 'react'
import { api, type DuctorStatus } from '../api/client'

function formatUptime(s: number): string {
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  return h > 0 ? `${h}h ${m}m` : `${m}m`
}

export function StatusPage() {
  const [data, setData] = useState<DuctorStatus | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    const load = () =>
      api.status().then(setData).catch((e: Error) => setError(e.message))
    load()
    const id = setInterval(load, 5000)
    return () => clearInterval(id)
  }, [])

  if (error) {
    return (
      <div className="p-6">
        <p className="text-red-400 font-mono text-sm">{error}</p>
        <p className="text-zinc-500 font-mono text-xs mt-2">
          Set your token: <code>localStorage.setItem('ductor_token', 'your-token')</code>
        </p>
      </div>
    )
  }

  if (!data) {
    return <div className="p-6 text-zinc-500 font-mono text-sm animate-pulse">Loading...</div>
  }

  const stats: [string, string][] = [
    ['Provider', data.provider],
    ['Model', data.model],
    ['Uptime', formatUptime(data.uptime_seconds)],
    ['API Connections', String(data.connections)],
    ['Dashboard Connections', String(data.dashboard_connections)],
    ['Status', data.status],
  ]

  return (
    <div className="p-6 space-y-6">
      <h1 className="text-lg font-mono font-semibold text-zinc-200">Bot Status</h1>
      <div className="grid grid-cols-2 gap-3">
        {stats.map(([label, value]) => (
          <div key={label} className="bg-zinc-900 rounded-lg p-4 border border-zinc-800">
            <div className="text-xs text-zinc-500 font-mono mb-1">{label}</div>
            <div className="text-sm text-zinc-100 font-mono">{value}</div>
          </div>
        ))}
      </div>
    </div>
  )
}
