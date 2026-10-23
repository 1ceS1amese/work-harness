import type { ModelVerification, ModelVerificationRequest } from '../types'
import type {
  ApplicationSettingsDescriptor,
  BlackboardContent,
  BlackboardHistoryPage,
  BlackboardObjectDetail,
  BlackboardObjectPage,
  BlackboardObjectQuery,
  BlackboardSummary,
  BoardEvent,
  CapabilitiesResponse,
  EditableRuntimeSettings,
  HealthResponse,
  ModelDiscoveryRequest,
  ModelDiscoveryResult,
  ProviderList,
  ProviderProfileSpec,
  ProviderSaved,
  RunAccepted,
  RunResult,
  RunList,
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
    readonly code?: string,
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
    let code: string | undefined
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
      } else if (body.detail && typeof body.detail === 'object') {
        const detail = body.detail as { code?: unknown; reason?: unknown }
        if (typeof detail.code === 'string') code = detail.code
        if (code === 'blackboard_not_initialized') message = '黑板正在初始化，请稍后刷新。'
        else if (code === 'snapshot_required') message = '当前快照已不可用，请刷新完整黑板。'
        else if (typeof detail.reason === 'string') message = detail.reason
      }
    } catch {
      // Non-JSON gateway errors retain the safe status-only message.
    }
    throw new ApiError(message, response.status, code)
  }

  if (response.status === 204) return undefined as T
  return (await response.json()) as T
}

function dispatchSseBlock(
  block: string,
  onRun: (run: RunResult) => void,
  onBlackboard: (event: BoardEvent) => void,
  onError: () => void,
): void {
  let eventName = 'message'
  const data: string[] = []
  for (const line of block.split(/\r?\n/)) {
    if (line.startsWith('event:')) eventName = line.slice(6).trim()
    if (line.startsWith('data:')) data.push(line.slice(5).trimStart())
  }
  if (eventName === 'run' && data.length > 0) {
    onRun(JSON.parse(data.join('\n')) as RunResult)
  }
  if (eventName === 'blackboard' && data.length > 0) {
    onBlackboard(JSON.parse(data.join('\n')) as BoardEvent)
  }
  if (eventName === 'snapshot_required') onError()
}

async function watchAuthenticatedRun(
  url: string,
  controller: AbortController,
  onRun: (run: RunResult) => void,
  onError: () => void,
  onBlackboard: (event: BoardEvent) => void,
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
        if (controller.signal.aborted) return
        if (block) dispatchSseBlock(block, onRun, onBlackboard, onError)
        separator = buffer.match(/\r?\n\r?\n/)
      }
      if (done) break
    }
    if (buffer.trim() && !controller.signal.aborted)
      dispatchSseBlock(buffer, onRun, onBlackboard, onError)
    // A reverse proxy can end a streaming response cleanly without surfacing a
    // fetch error. Let the caller fall back to polling unless it deliberately
    // closed the stream after receiving a terminal snapshot.
    if (!controller.signal.aborted) onError()
  } catch (error) {
    if (!controller.signal.aborted) onError()
  }
}

function queryString(params: object): string {
  const query = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null) query.set(key, String(value))
  }
  const value = query.toString()
  return value ? `?${value}` : ''
}

function boardPath(runId: string): string {
  return `/v1/runs/${encodeURIComponent(runId)}/blackboard`
}

export const harnessApi = {
  baseUrl: () => new URL(`${API_BASE}/`, window.location.href).href.replace(/\/$/, ''),
  setApiKey: (value: string) => writeApiKey(value.trim()),
  clearApiKey: () => writeApiKey(''),
  hasApiKey: () => Boolean(readApiKey()),
  health: (signal?: AbortSignal) => request<HealthResponse>('/health', { signal }),
  capabilities: (signal?: AbortSignal) =>
    request<CapabilitiesResponse>('/v1/capabilities', { signal }),
  discoverModels: (payload: ModelDiscoveryRequest, signal?: AbortSignal) =>
    request<ModelDiscoveryResult>('/v1/providers/models/discover', {
      method: 'POST',
      body: JSON.stringify(payload),
      signal,
    }),
  verifyModel: (payload: ModelVerificationRequest, signal?: AbortSignal) =>
    request<ModelVerification>('/v1/providers/models/verify', {
      method: 'POST',
      body: JSON.stringify(payload),
      signal,
    }),
  providers: (signal?: AbortSignal) => request<ProviderList>('/v1/providers', { signal }),
  saveProvider: (name: string, profile: ProviderProfileSpec, signal?: AbortSignal) =>
    request<ProviderSaved>(`/v1/providers/${encodeURIComponent(name)}`, {
      method: 'PUT',
      body: JSON.stringify(profile),
      signal,
    }),
  deleteProvider: (name: string) =>
    request<void>(`/v1/providers/${encodeURIComponent(name)}`, { method: 'DELETE' }),
  settings: (signal?: AbortSignal) =>
    request<ApplicationSettingsDescriptor>('/v1/settings', { signal }),
  savePrompt: (name: string, body: string) =>
    request<ApplicationSettingsDescriptor>(`/v1/settings/prompts/${encodeURIComponent(name)}`, {
      method: 'PUT',
      body: JSON.stringify({ body }),
    }),
  resetPrompt: (name: string) =>
    request<ApplicationSettingsDescriptor>(`/v1/settings/prompts/${encodeURIComponent(name)}`, {
      method: 'DELETE',
    }),
  saveRuntimeSettings: (settings: EditableRuntimeSettings) =>
    request<ApplicationSettingsDescriptor>('/v1/settings/runtime', {
      method: 'PUT',
      body: JSON.stringify(settings),
    }),
  createRun: (payload: TaskRequest, signal?: AbortSignal) =>
    request<RunAccepted>('/v1/runs', {
      method: 'POST',
      body: JSON.stringify(payload),
      signal,
    }),
  getRun: (runId: string, signal?: AbortSignal) =>
    request<RunResult>(`/v1/runs/${encodeURIComponent(runId)}`, { signal }),
  listRuns: (offset = 0, signal?: AbortSignal) =>
    request<RunList>(`/v1/runs?limit=20&offset=${offset}`, { signal }),
  blackboard: (runId: string, signal?: AbortSignal, atRevision?: number) =>
    request<BlackboardSummary>(`${boardPath(runId)}${queryString({ at_revision: atRevision })}`, {
      signal,
    }),
  blackboardObjects: (runId: string, query: BlackboardObjectQuery, signal?: AbortSignal) =>
    request<BlackboardObjectPage>(`${boardPath(runId)}/objects${queryString(query)}`, { signal }),
  blackboardObject: (
    runId: string,
    ref: { object_id: string; revision: number },
    atRevision: number,
    signal?: AbortSignal,
  ) =>
    request<BlackboardObjectDetail>(
      `${boardPath(runId)}/objects/${encodeURIComponent(ref.object_id)}${queryString({ object_revision: ref.revision, at_revision: atRevision })}`,
      { signal },
    ),
  blackboardContent: (runId: string, digest: string, atRevision: number, signal?: AbortSignal) =>
    request<BlackboardContent>(
      `${boardPath(runId)}/content/${encodeURIComponent(digest)}?at_revision=${atRevision}`,
      { signal },
    ),
  blackboardHistory: (
    runId: string,
    objectId: string,
    query: { at_revision?: number; cursor?: string; limit?: number },
    signal?: AbortSignal,
  ) =>
    request<BlackboardHistoryPage>(
      `${boardPath(runId)}/objects/${encodeURIComponent(objectId)}/revisions${queryString(query)}`,
      { signal },
    ),
  resumeRun: (runId: string, approved: boolean, note?: string) =>
    request<{ run_id: string; status: string }>(`/v1/runs/${encodeURIComponent(runId)}/resume`, {
      method: 'POST',
      body: JSON.stringify({ approved, note: note || null }),
    }),
  cancelRun: (runId: string) =>
    request<void>(`/v1/runs/${encodeURIComponent(runId)}`, { method: 'DELETE' }),
  watchRun: (
    runId: string,
    onRun: (run: RunResult) => void,
    onError: () => void,
    onBlackboard: (event: BoardEvent) => void = onError,
    after?: string,
  ): (() => void) => {
    const url = `${API_BASE}/v1/runs/${encodeURIComponent(runId)}/events${queryString({ after })}`
    if (readApiKey()) {
      const controller = new AbortController()
      void watchAuthenticatedRun(url, controller, onRun, onError, onBlackboard)
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
    source.addEventListener('blackboard', (event) => {
      try {
        onBlackboard(JSON.parse((event as MessageEvent<string>).data) as BoardEvent)
      } catch {
        source.close()
        onError()
      }
    })
    source.addEventListener('snapshot_required', () => {
      source.close()
      onError()
    })
    source.onerror = () => {
      source.close()
      onError()
    }
    return () => source.close()
  },
}
