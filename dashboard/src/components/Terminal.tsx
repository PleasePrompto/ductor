import { useEffect, useRef } from 'react'

interface Props {
  chunk: string | null
  clearSignal?: number
}

export function Terminal({ chunk, clearSignal }: Props) {
  const preRef = useRef<HTMLPreElement>(null)
  const bottomRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (chunk === null || !preRef.current) return
    preRef.current.appendChild(document.createTextNode(chunk))
    bottomRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' })
  }, [chunk])

  useEffect(() => {
    if (clearSignal !== undefined && preRef.current) {
      preRef.current.textContent = ''
    }
  }, [clearSignal])

  return (
    <div className="h-full bg-zinc-950 rounded-lg border border-zinc-800 flex flex-col overflow-hidden">
      <div className="flex items-center gap-2 px-4 py-2 bg-zinc-900 border-b border-zinc-800 shrink-0">
        <span className="w-3 h-3 rounded-full bg-red-500" />
        <span className="w-3 h-3 rounded-full bg-yellow-500" />
        <span className="w-3 h-3 rounded-full bg-green-500" />
        <span className="ml-2 text-xs text-zinc-500 font-mono">live output</span>
      </div>
      <pre
        ref={preRef}
        className="flex-1 overflow-y-auto p-4 font-mono text-sm text-green-400 leading-relaxed whitespace-pre-wrap break-words"
      />
      <div ref={bottomRef} />
    </div>
  )
}
