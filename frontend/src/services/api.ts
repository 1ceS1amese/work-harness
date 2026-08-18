import type {
  CapabilitiesResponse,
  HealthResponse,
  ProviderList,
  ProviderProfileSpec,
  ProviderSaved,
  RunAccepted,
  RunResult,
  TaskRequest,
} from '../types'

const API_BASE = (import.meta.env.VITE_API_BASE_URL as string | undefined)?.replace(/\/$/, '') ?? ''
const API_KEY_STORAGE_KEY = 'work-harness:api-key'
let memoryApiKey = ''

function readApiKey(): string {
  try {
    return window.sessionStorage.getItem(API_KEY_STORAGE_KEY) ?? memoryApiKey
  } catch {
    return memoryApiKey
  }
}

function writeApiKey(value: string): void {
  memoryApiKey = value
  try {
    if (value) window.sessionStorage.setItem(API_KEY_STORAGE_KEY, value)
    else window.sessionStorage.removeItem(API_KEY_STORAGE_KEY)
  } catch {
    // Sandboxed browsers can reject storage; the in-memory fallback still works.
  }
}

function requestHeaders(init?: RequestInit): Headers {
  const headers = new Headers(init?.headers)
  headers.set('Accept', 'application/json')
  if (init?.body) headers.set('Content-Type', 'application/json')
  const apiKey = readApiKey()
  if (apiKey) headers.set('X-API-Key', apiKey)
  return headers
}

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message)
    this.name = 'ApiError'
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: requestHeaders(init),
  })

  if (!response.ok) {
    let message = `请求失败（${response.status}）`
    try {
      const body = (await response.json()) as { detail?: unknown }
      if (typeof body.detail === 'string' && body.detail) message = body.detail
      else if (Array.isArray(body.detail)) {
        const issues = body.detail
          .map((item) => {
            if (!item || typeof item !== 'object') return ''
            const issue = item as { loc?: unknown; msg?: unknown }
            const location = Array.isArray(issue.loc) ? issue.loc.slice(1).join('.') : ''
            const detail = typeof issue.msg === 'string' ? issue.msg : ''
            return [location, detail].filter(Boolean).join('：')
          })
          .filter(Boolean)
        if (issues.length > 0) message = issues.join('；')
      }
    } catch {
      // Non-JSON gateway errors retain the safe status-only message.
    }
    throw new ApiError(message, response.status)
  }

  if (response.status === 204) return undefined as T
  return (await response.json()) as T
}

function dispatchSseBlock(block: string, onRun: (run: RunResult) => void): void {
  let eventName = 'message'
  const data: string[] = []
  for (const line of block.split(/\r?\n/)) {
    if (line.startsWith('event:')) eventName = line.slice(6).trim()
    if (line.startsWith('data:')) data.push(line.slice(5).trimStart())
  }
  if (eventName === 'run' && data.length > 0) {
    onRun(JSON.parse(data.join('\n')) as RunResult)
  }
}

async function watchAuthenticatedRun(
  url: string,
  controller: AbortController,
  onRun: (run: RunResult) => void,
  onError: () => void,
): Promise<void> {
  try {
    const response = await fetch(url, {
      headers: (() => {
        const headers = requestHeaders()
        headers.set('Accept', 'text/event-stream')
        return headers
      })(),
      signal: controller.signal,
    })
    if (!response.ok || !response.body) throw new ApiError('流式连接失败', response.status)

    const reader = response.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''
    while (true) {
      const { done, value } = await reader.read()
      buffer += decoder.decode(value, { stream: !done })
      let separator = buffer.match(/\r?\n\r?\n/)
      while (separator?.index !== undefined) {
        const block = buffer.slice(0, separator.index)
        buffer = buffer.slice(separator.index + separator[0].length)
        if (block) dispatchSseBlock(block, onRun)
        separator = buffer.match(/\r?\n\r?\n/)
      }
      if (done) break
    }
    if (buffer.trim()) dispatchSseBlock(buffer, onRun)
  } catch (error) {
    if (!controller.signal.aborted) onError()
  }
}

export const harnessApi = {
  setApiKey: (value: string) => writeApiKey(value.trim()),
  clearApiKey: () => writeApiKey(''),
  hasApiKey: () => Boolean(readApiKey()),
  health: (signal?: AbortSignal) => request<HealthResponse>('/health', { signal }),
  capabilities: (signal?: AbortSignal) =>
    request<CapabilitiesResponse>('/v1/capabilities', { signal }),
  providers: (signal?: AbortSignal) => request<ProviderList>('/v1/providers', { signal }),
  saveProvider: (name: string, profile: ProviderProfileSpec, signal?: AbortSignal) =>
    request<ProviderSaved>(`/v1/providers/${encodeURIComponent(name)}`, {
      method: 'PUT',
      body: JSON.stringify(profile),
      signal,
    }),
  deleteProvider: (name: string) =>
    request<void>(`/v1/providers/${encodeURIComponent(name)}`, { method: 'DELETE' }),
  createRun: (payload: TaskRequest, signal?: AbortSignal) =>
    request<RunAccepted>('/v1/runs', {
      method: 'POST',
      body: JSON.stringify(payload),
      signal,
    }),
  getRun: (runId: string, signal?: AbortSignal) =>
    request<RunResult>(`/v1/runs/${encodeURIComponent(runId)}`, { signal }),
  resumeRun: (runId: string, approved: boolean, note?: string) =>
    request<{ run_id: string; status: string }>(
      `/v1/runs/${encodeURIComponent(runId)}/resume`,
      {
        method: 'POST',
        body: JSON.stringify({ approved, note: note || null }),
      },
    ),
  cancelRun: (runId: string) =>
    request<void>(`/v1/runs/${encodeURIComponent(runId)}`, { method: 'DELETE' }),
  watchRun: (
    runId: string,
    onRun: (run: RunResult) => void,
    onError: () => void,
  ): (() => void) => {
    const url = `${API_BASE}/v1/runs/${encodeURIComponent(runId)}/events`
    if (readApiKey()) {
      const controller = new AbortController()
      void watchAuthenticatedRun(url, controller, onRun, onError)
      return () => controller.abort()
    }
    const source = new EventSource(url)
    source.addEventListener('run', (event) => {
      try {
        onRun(JSON.parse((event as MessageEvent<string>).data) as RunResult)
      } catch {
        source.close()
        onError()
      }
    })
    source.onerror = () => {
      source.close()
      onError()
    }
    return () => source.close()
  },
}
