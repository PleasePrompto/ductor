const BASE = '/dashboard/api'

export function getToken(): string {
  return localStorage.getItem('ductor_token') ?? ''
}

export function setToken(token: string): void {
  localStorage.setItem('ductor_token', token)
}

export async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${getToken()}`,
      ...init?.headers,
    },
  })
  if (res.status === 401) throw new Error('Unauthorized — check your API token')
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`)
  return res.json() as Promise<T>
}

export interface DuctorStatus {
  status: string
  provider: string
  model: string
  uptime_seconds: number
  connections: number
  dashboard_connections: number
}

export interface SessionData {
  transport: string
  chat_id: number
  topic_id: number | null
  provider: string
  model: string
  created_at: string
  last_active: string
  provider_sessions: Record<string, { message_count: number; total_cost_usd: number }>
}

export interface TaskEntry {
  task_id: string
  status: 'running' | 'done' | 'failed' | 'cancelled' | 'waiting'
  prompt: string
  created_at: string
  chat_id: number
}

export interface LogFile {
  name: string
  size: number
  mtime: number
}

export const api = {
  status: () => apiFetch<DuctorStatus>('/status'),
  sessions: () => apiFetch<Record<string, SessionData>>('/sessions'),
  tasks: () => apiFetch<TaskEntry[]>('/tasks'),
  logsList: () => apiFetch<LogFile[]>('/logs'),
  logsFile: (name: string) =>
    fetch(`${BASE}/logs/${encodeURIComponent(name)}`, {
      headers: { Authorization: `Bearer ${getToken()}` },
    }),
}
