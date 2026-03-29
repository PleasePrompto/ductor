import { useEffect, useState } from 'react'
import { api, type LogFile } from '../api/client'

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes}B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)}KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)}MB`
}

export function LogsPage() {
  const [files, setFiles] = useState<LogFile[]>([])
  const [selected, setSelected] = useState<string | null>(null)
  const [content, setContent] = useState<string>('')
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    api.logsList().then(setFiles).catch(console.error)
  }, [])

  const openLog = async (name: string) => {
    setSelected(name)
    setLoading(true)
    try {
      const res = await api.logsFile(name)
      setContent(await res.text())
    } catch (e) {
      setContent(String(e))
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="flex h-full">
      <aside className="w-60 shrink-0 border-r border-zinc-800 overflow-y-auto p-2 space-y-1">
        <p className="text-xs text-zinc-600 font-mono px-2 mb-2 uppercase tracking-wider">
          Log Files
        </p>
        {files.length === 0 && (
          <p className="text-xs text-zinc-600 font-mono px-2">No log files found</p>
        )}
        {files.map((f) => (
          <button
            key={f.name}
            onClick={() => openLog(f.name)}
            className={`w-full text-left px-3 py-2 rounded text-xs font-mono transition-colors ${
              selected === f.name
                ? 'bg-zinc-800 text-zinc-200 border border-zinc-700'
                : 'text-zinc-400 hover:bg-zinc-900 hover:text-zinc-300'
            }`}
          >
            <div className="truncate">{f.name}</div>
            <div className="text-zinc-600 mt-0.5">{formatSize(f.size)}</div>
          </button>
        ))}
      </aside>

      <main className="flex-1 min-w-0 overflow-auto p-4">
        {!selected && (
          <p className="text-sm text-zinc-600 font-mono">Select a log file to view</p>
        )}
        {selected && loading && (
          <p className="text-sm text-zinc-500 font-mono animate-pulse">Loading...</p>
        )}
        {selected && !loading && (
          <pre className="text-xs font-mono text-zinc-400 whitespace-pre-wrap break-words leading-relaxed">
            {content}
          </pre>
        )}
      </main>
    </div>
  )
}
