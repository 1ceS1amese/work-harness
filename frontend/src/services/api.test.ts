import { afterEach, describe, expect, it, vi } from 'vitest'

import { harnessApi } from './api'

afterEach(() => {
  harnessApi.clearApiKey()
  vi.unstubAllGlobals()
})

describe('harness api client', () => {
  it('submits a structured task contract', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ run_id: 'run_1', status_url: '/v1/runs/run_1' }), {
        status: 202,
        headers: { 'Content-Type': 'application/json' },
      }),
    )
    vi.stubGlobal('fetch', fetchMock)

    await expect(
      harnessApi.createRun({
        goal: '完成架构',
        task_type: 'planning',
        risk_level: 'low',
        assignment: {
          mode: 'cross_functional',
          assignees: ['worker-01', 'worker-02'],
          functions_by_agent: {
            'worker-01': ['analysis'],
            'worker-02': ['execution'],
          },
          cross_review: true,
        },
      }),
    ).resolves.toEqual({ run_id: 'run_1', status_url: '/v1/runs/run_1' })

    expect(fetchMock).toHaveBeenCalledWith(
      '/v1/runs',
      expect.objectContaining({ method: 'POST' }),
    )
    const init = fetchMock.mock.calls[0]?.[1] as RequestInit
    expect(JSON.parse(init.body as string)).toMatchObject({
      goal: '完成架构',
      task_type: 'planning',
      risk_level: 'low',
      assignment: {
        mode: 'cross_functional',
        assignees: ['worker-01', 'worker-02'],
        functions_by_agent: {
          'worker-01': ['analysis'],
          'worker-02': ['execution'],
        },
        cross_review: true,
      },
    })
  })

  it('keeps the backend detail when a request fails', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ detail: 'run not found' }), {
          status: 404,
          headers: { 'Content-Type': 'application/json' },
        }),
      ),
    )

    await expect(harnessApi.getRun('missing')).rejects.toEqual(
      expect.objectContaining({ status: 404, message: 'run not found', name: 'ApiError' }),
    )
  })

  it('sends the deployment API key from session storage', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ status: 'ok' }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    )
    vi.stubGlobal('fetch', fetchMock)
    harnessApi.setApiKey('deployment-secret')

    await harnessApi.health()

    const init = fetchMock.mock.calls[0]?.[1] as RequestInit
    expect(new Headers(init.headers).get('X-API-Key')).toBe('deployment-secret')
    expect(harnessApi.hasApiKey()).toBe(true)
  })

  it('formats structured validation failures', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            detail: [
              {
                loc: ['body', 'config', 'api_key_env'],
                msg: 'Field required',
                type: 'missing',
              },
            ],
          }),
          { status: 422, headers: { 'Content-Type': 'application/json' } },
        ),
      ),
    )

    await expect(
      harnessApi.saveProvider('team-model', { config: { adapter: 'openai_compatible' } }),
    ).rejects.toEqual(
      expect.objectContaining({
        status: 422,
        message: 'config.api_key_env：Field required',
      }),
    )
  })

  it('persists a provider profile without sending a credential value', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          provider: { name: 'team-model' },
          restart_required: true,
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    )
    vi.stubGlobal('fetch', fetchMock)

    await harnessApi.saveProvider('team-model', {
      config: {
        adapter: 'openai_compatible',
        api_key_env: 'TEAM_MODEL_API_KEY',
        allowed_models: ['main-model'],
      },
      agent_bindings: { main: 'main-model' },
    })

    const init = fetchMock.mock.calls[0]?.[1] as RequestInit
    const body = JSON.parse(init.body as string)
    expect(body.config.api_key_env).toBe('TEAM_MODEL_API_KEY')
    expect(JSON.stringify(body)).not.toContain('sk-')
    expect(fetchMock).toHaveBeenCalledWith(
      '/v1/providers/team-model',
      expect.objectContaining({ method: 'PUT' }),
    )
  })

  it('sends a UI-managed Provider credential only in the save request', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({ provider: { name: 'ui-model' }, restart_required: true }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    )
    vi.stubGlobal('fetch', fetchMock)

    await harnessApi.saveProvider('ui-model', {
      config: {
        adapter: 'openai_compatible',
        credential_source: 'stored',
        allowed_models: ['main-model'],
      },
      api_key: 'ui-secret-value',
    })

    const body = JSON.parse(fetchMock.mock.calls[0]?.[1]?.body as string)
    expect(body.api_key).toBe('ui-secret-value')
    expect(body.config.api_key_env).toBeUndefined()
  })

  it('streams run snapshots and closes the event source', () => {
    let instance: FakeEventSource | undefined
    class FakeEventSource {
      onerror: (() => void) | null = null
      closed = false
      private listener: ((event: { data: string }) => void) | undefined

      constructor(readonly url: string) {
        instance = this
      }

      addEventListener(_: string, listener: (event: { data: string }) => void): void {
        this.listener = listener
      }

      emit(data: string): void {
        this.listener?.({ data })
      }

      close(): void {
        this.closed = true
      }
    }
    vi.stubGlobal('EventSource', FakeEventSource)
    const onRun = vi.fn()
    const stop = harnessApi.watchRun('run_1', onRun, vi.fn())

    expect(instance?.url).toBe('/v1/runs/run_1/events')
    instance?.emit('{"run_id":"run_1","status":"running"}')
    expect(onRun).toHaveBeenCalledWith(
      expect.objectContaining({ run_id: 'run_1', status: 'running' }),
    )
    stop()
    expect(instance?.closed).toBe(true)
  })

  it('streams with fetch when API authentication is enabled', async () => {
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(
          new TextEncoder().encode(
            'event: run\ndata: {"run_id":"run_2","status":"running"}\n\n',
          ),
        )
        controller.close()
      },
    })
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(stream, { status: 200, headers: { 'Content-Type': 'text/event-stream' } }),
    )
    vi.stubGlobal('fetch', fetchMock)
    harnessApi.setApiKey('deployment-secret')
    const onRun = vi.fn()

    const stop = harnessApi.watchRun('run_2', onRun, vi.fn())

    await vi.waitFor(() => expect(onRun).toHaveBeenCalledOnce())
    const init = fetchMock.mock.calls[0]?.[1] as RequestInit
    expect(new Headers(init.headers).get('X-API-Key')).toBe('deployment-secret')
    expect(onRun).toHaveBeenCalledWith(
      expect.objectContaining({ run_id: 'run_2', status: 'running' }),
    )
    stop()
  })
})
