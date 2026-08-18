import type {
  CapabilitiesResponse,
  HealthResponse,
  RunAccepted,
  RunResult,
  TaskRequest,
} from '../types'

const API_BASE = (import.meta.env.VITE_API_BASE_URL as string | undefined)?.replace(/\/$/, '') ?? ''

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
    headers: {
      Accept: 'application/json',
      ...(init?.body ? { 'Content-Type': 'application/json' } : {}),
      ...init?.headers,
    },
  })

  if (!response.ok) {
    let message = `请求失败（${response.status}）`
    try {
      const body = (await response.json()) as { detail?: string }
      if (body.detail) message = body.detail
    } catch {
      // Non-JSON gateway errors retain the safe status-only message.
    }
    throw new ApiError(message, response.status)
  }

  if (response.status === 204) return undefined as T
  return (await response.json()) as T
}

export const harnessApi = {
  health: (signal?: AbortSignal) => request<HealthResponse>('/health', { signal }),
  capabilities: (signal?: AbortSignal) =>
    request<CapabilitiesResponse>('/v1/capabilities', { signal }),
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
    const source = new EventSource(
      `${API_BASE}/v1/runs/${encodeURIComponent(runId)}/events`,
    )
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
