import { useCallback, useState } from 'react'
import { useWebSocket } from '../hooks/useWebSocket'
import { Terminal } from '../components/Terminal'
import { StatusBadge } from '../components/StatusBadge'
import { getToken } from '../api/client'

export function StreamPage() {
  const [latestChunk, setLatestChunk] = useState<string | null>(null)
  const [clearSignal, setClearSignal] = useState(0)

  const handleMessage = useCallback((raw: string) => {
    try {
      const msg = JSON.parse(raw) as { type: string; data?: string }
      if (msg.type === 'text_delta' && msg.data) {
        setLatestChunk(msg.data)
      }
    } catch {
      // ignore malformed frames
    }
  }, [])

  const { status } = useWebSocket('/dashboard/ws/stream', {
    onMessage: handleMessage,
    token: getToken(),
  })

  return (
    <div className="flex flex-col h-full gap-3 p-4">
      <div className="flex items-center justify-between shrink-0">
        <StatusBadge status={status} />
        <button
          onClick={() => setClearSignal((s) => s + 1)}
          className="px-3 py-1 text-xs font-mono bg-zinc-800 hover:bg-zinc-700 text-zinc-300 rounded border border-zinc-700 transition-colors"
        >
          clear
        </button>
      </div>
      <div className="flex-1 min-h-0">
        <Terminal chunk={latestChunk} clearSignal={clearSignal} />
      </div>
    </div>
  )
}
