import { useCallback, useEffect, useRef, useState } from 'react'

export type WsStatus = 'connecting' | 'connected' | 'reconnecting' | 'disconnected'

interface Options {
  onMessage: (data: string) => void
  token?: string
  maxAttempts?: number
  initialDelay?: number
  maxDelay?: number
}

export function useWebSocket(url: string, options: Options) {
  const {
    onMessage,
    token,
    maxAttempts = 10,
    initialDelay = 1000,
    maxDelay = 30_000,
  } = options

  const [status, setStatus] = useState<WsStatus>('connecting')
  const wsRef = useRef<WebSocket | null>(null)
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const attemptsRef = useRef(0)
  const unmountedRef = useRef(false)
  const onMessageRef = useRef(onMessage)
  useEffect(() => { onMessageRef.current = onMessage }, [onMessage])

  const connect = useCallback(() => {
    if (unmountedRef.current) return
    const wsUrl = token ? `${url}?token=${encodeURIComponent(token)}` : url
    setStatus(attemptsRef.current === 0 ? 'connecting' : 'reconnecting')

    const ws = new WebSocket(wsUrl)
    wsRef.current = ws

    ws.onopen = () => {
      if (unmountedRef.current) { ws.close(); return }
      attemptsRef.current = 0
      setStatus('connected')
    }

    ws.onmessage = (e: MessageEvent) => onMessageRef.current(e.data as string)

    ws.onerror = () => ws.close()

    ws.onclose = () => {
      if (unmountedRef.current) return
      if (attemptsRef.current >= maxAttempts) {
        setStatus('disconnected')
        return
      }
      const delay = Math.min(initialDelay * 2 ** attemptsRef.current, maxDelay)
      attemptsRef.current++
      timerRef.current = setTimeout(connect, delay)
    }
  }, [url, token, maxAttempts, initialDelay, maxDelay])

  useEffect(() => {
    unmountedRef.current = false
    attemptsRef.current = 0
    connect()
    return () => {
      unmountedRef.current = true
      if (timerRef.current) clearTimeout(timerRef.current)
      wsRef.current?.close()
    }
  }, [connect])

  return { status }
}
