import type { WsStatus } from '../hooks/useWebSocket'

const MAP: Record<WsStatus, { dot: string; text: string; label: string }> = {
  connected:    { dot: 'bg-green-500',               text: 'text-green-400',  label: 'Connected'    },
  connecting:   { dot: 'bg-yellow-500 animate-pulse', text: 'text-yellow-400', label: 'Connecting'   },
  reconnecting: { dot: 'bg-orange-500 animate-pulse', text: 'text-orange-400', label: 'Reconnecting' },
  disconnected: { dot: 'bg-red-500',                 text: 'text-red-400',    label: 'Disconnected' },
}

export function StatusBadge({ status }: { status: WsStatus }) {
  const s = MAP[status]
  return (
    <div className={`flex items-center gap-2 text-xs font-mono ${s.text}`}>
      <span className={`w-2 h-2 rounded-full ${s.dot}`} />
      {s.label}
    </div>
  )
}
