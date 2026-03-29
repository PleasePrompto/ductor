import { useEffect, useState } from 'react'
import { api, type SessionData, type TaskEntry } from '../api/client'

const STATUS_COLOR: Record<string, string> = {
  running:   'text-green-400 bg-green-400/10 border-green-400/20',
  done:      'text-zinc-400 bg-zinc-400/10 border-zinc-400/20',
  failed:    'text-red-400 bg-red-400/10 border-red-400/20',
  cancelled: 'text-zinc-600 bg-zinc-600/10 border-zinc-600/20',
  waiting:   'text-yellow-400 bg-yellow-400/10 border-yellow-400/20',
}

export function SessionsPage() {
  const [sessions, setSessions] = useState<Record<string, SessionData>>({})
  const [tasks, setTasks] = useState<TaskEntry[]>([])

  useEffect(() => {
    const load = () => {
      api.sessions().then(setSessions).catch(console.error)
      api.tasks().then(setTasks).catch(console.error)
    }
    load()
    const id = setInterval(load, 3000)
    return () => clearInterval(id)
  }, [])

  return (
    <div className="p-6 space-y-8 overflow-auto">
      <section>
        <h2 className="text-base font-mono font-semibold text-zinc-300 mb-3">Tasks</h2>
        {tasks.length === 0 ? (
          <p className="text-sm text-zinc-600 font-mono">No active tasks</p>
        ) : (
          <div className="space-y-2">
            {tasks.map((t) => (
              <div
                key={t.task_id}
                className="bg-zinc-900 rounded-lg p-3 border border-zinc-800 flex items-center gap-3"
              >
                <span
                  className={`text-xs font-mono px-2 py-0.5 rounded border ${STATUS_COLOR[t.status] ?? 'text-zinc-400 bg-zinc-400/10 border-zinc-400/20'}`}
                >
                  {t.status}
                </span>
                <span className="text-sm text-zinc-300 font-mono truncate flex-1">{t.prompt}</span>
                <span className="text-xs text-zinc-600 font-mono shrink-0">
                  {new Date(t.created_at).toLocaleTimeString()}
                </span>
              </div>
            ))}
          </div>
        )}
      </section>

      <section>
        <h2 className="text-base font-mono font-semibold text-zinc-300 mb-3">Sessions</h2>
        {Object.keys(sessions).length === 0 ? (
          <p className="text-sm text-zinc-600 font-mono">No sessions found</p>
        ) : (
          <div className="space-y-2">
            {Object.entries(sessions).map(([key, s]) => (
              <div key={key} className="bg-zinc-900 rounded-lg p-3 border border-zinc-800">
                <div className="flex items-center justify-between mb-1">
                  <span className="text-xs text-zinc-400 font-mono font-semibold">{key}</span>
                  <span className="text-xs text-zinc-500 font-mono">
                    {s.provider} / {s.model}
                  </span>
                </div>
                <div className="text-xs text-zinc-600 font-mono">
                  Last active: {new Date(s.last_active).toLocaleString()}
                </div>
                {s.provider_sessions && Object.entries(s.provider_sessions).map(([p, ps]) => (
                  <div key={p} className="text-xs text-zinc-600 font-mono mt-0.5">
                    {p}: {ps.message_count} msgs · ${ps.total_cost_usd.toFixed(4)}
                  </div>
                ))}
              </div>
            ))}
          </div>
        )}
      </section>
    </div>
  )
}
