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

  it('saves prompt and runtime settings through dedicated endpoints', async () => {
    const fetchMock = vi.fn().mockImplementation(() => Promise.resolve(
      new Response(JSON.stringify({ prompts: [], runtime: {}, restart_required: true }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    ))
    vi.stubGlobal('fetch', fetchMock)

    await harnessApi.savePrompt('planner', '先确认目标，再安排任务。')
    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      '/v1/settings/prompts/planner',
      expect.objectContaining({ method: 'PUT' }),
    )
    expect(JSON.parse(fetchMock.mock.calls[0]?.[1]?.body as string)).toEqual({
      body: '先确认目标，再安排任务。',
    })

    await harnessApi.saveRuntimeSettings({
      run_max_concurrency: 2,
      run_queue_capacity: 8,
      run_queue_retry_after_seconds: 5,
      worker_max_concurrency: 4,
      reviewer_max_concurrency: 3,
      review_strategy: 'adaptive',
      deep_review_risk_levels: ['high', 'critical'],
      deep_review_task_types: ['research', 'decision'],
      max_planned_subtasks: 4,
      max_repair_rounds: 1,
      node_timeout_seconds: 60,
      run_timeout_seconds: 300,
      max_artifact_chars: 20000,
      max_tool_result_chars: 8000,
      model_retry_attempts: 2,
    })
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      '/v1/settings/runtime',
      expect.objectContaining({ method: 'PUT' }),
    )
    expect(JSON.parse(fetchMock.mock.calls[1]?.[1]?.body as string)).toMatchObject({
      run_max_concurrency: 2,
      review_strategy: 'adaptive',
    })
  })

  it('streams run snapshots and closes the event source', () => {
    let instance: FakeEventSource | undefined
    class FakeEventSource {
      onerror: (() => void) | null = null
      closed = false
      private listeners = new Map<string, (event: { data: string }) => void>()

      constructor(readonly url: string) {
        instance = this
      }

      addEventListener(name: string, listener: (event: { data: string }) => void): void {
        this.listeners.set(name, listener)
      }

      emit(data: string, name = 'run'): void {
        this.listeners.get(name)?.({ data })
      }

      close(): void {
        this.closed = true
      }
    }
    vi.stubGlobal('EventSource', FakeEventSource)
    const onRun = vi.fn()
    const onBlackboard = vi.fn()
    const onError = vi.fn()
    const stop = harnessApi.watchRun('run_1', onRun, onError, onBlackboard)

    expect(instance?.url).toBe('/v1/runs/run_1/events')
    instance?.emit('{"run_id":"run_1","status":"running"}')
    expect(onRun).toHaveBeenCalledWith(
      expect.objectContaining({ run_id: 'run_1', status: 'running' }),
    )
    instance?.emit('{"board_id":"run_1","sequence":3}', 'blackboard')
    expect(onBlackboard).toHaveBeenCalledWith({ board_id: 'run_1', sequence: 3 })
    expect(onRun).toHaveBeenCalledOnce()
    instance?.emit('{}', 'snapshot_required')
    expect(onError).toHaveBeenCalledOnce()
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
    const onError = vi.fn()

    const stop = harnessApi.watchRun('run_2', onRun, onError)

    await vi.waitFor(() => expect(onRun).toHaveBeenCalledOnce())
    await vi.waitFor(() => expect(onError).toHaveBeenCalledOnce())
    const init = fetchMock.mock.calls[0]?.[1] as RequestInit
    expect(new Headers(init.headers).get('X-API-Key')).toBe('deployment-secret')
    expect(onRun).toHaveBeenCalledWith(
      expect.objectContaining({ run_id: 'run_2', status: 'running' }),
    )
    stop()
  })

  it('encodes board references and pins every read to the requested snapshot', async () => {
    const fetchMock = vi.fn().mockImplementation(() => Promise.resolve(new Response('{}')))
    vi.stubGlobal('fetch', fetchMock)
    harnessApi.setApiKey('test-key')
    await harnessApi.blackboard('run ?')
    await harnessApi.blackboardObjects('run ?', { at_revision: 7, kind: 'fact', topic: 'a&b' })
    await harnessApi.blackboardObjects('run ?', { cursor: 'opaque+cursor/=', limit: 20 })
    await harnessApi.blackboardObject('run ?', { object_id: 'fact ?&', revision: 2 }, 7)
    await harnessApi.blackboardContent('run ?', 'abc', 7)
    await harnessApi.listRuns(20)
    const urls = fetchMock.mock.calls.map(call => call[0])
    expect(urls).toEqual([
      '/v1/runs/run%20%3F/blackboard',
      '/v1/runs/run%20%3F/blackboard/objects?at_revision=7&kind=fact&topic=a%26b',
      '/v1/runs/run%20%3F/blackboard/objects?cursor=opaque%2Bcursor%2F%3D&limit=20',
      '/v1/runs/run%20%3F/blackboard/objects/fact%20%3F%26?object_revision=2&at_revision=7',
      '/v1/runs/run%20%3F/blackboard/content/abc?at_revision=7', '/v1/runs?limit=20&offset=20',
    ])
    for (const call of fetchMock.mock.calls) expect(new Headers(call[1].headers).get('X-API-Key')).toBe('test-key')
  })

  it('encodes historical snapshot and object-history pagination queries', async () => {
    const fetchMock = vi.fn().mockImplementation(() => Promise.resolve(new Response('{}')))
    vi.stubGlobal('fetch', fetchMock)
    await harnessApi.blackboard('board', undefined, 3)
    await harnessApi.blackboardHistory('board', 'fact ?&', { at_revision: 7, limit: 20 })
    await harnessApi.blackboardHistory('board', 'fact ?&', { cursor: 'opaque+cursor/=' })
    expect(fetchMock.mock.calls.map(call => call[0])).toEqual([
      '/v1/runs/board/blackboard?at_revision=3',
      '/v1/runs/board/blackboard/objects/fact%20%3F%26/revisions?at_revision=7&limit=20',
      '/v1/runs/board/blackboard/objects/fact%20%3F%26/revisions?cursor=opaque%2Bcursor%2F%3D',
    ])
  })

  it.each([
    [409, 'blackboard_not_initialized', '黑板正在初始化'],
    [410, 'snapshot_required', '当前快照已不可用'],
  ])('preserves the structured read error %s/%s', async (status, code, message) => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: { code } }), { status: Number(status) })))
    await expect(harnessApi.blackboard('board')).rejects.toMatchObject({ status, code, message: expect.stringContaining(String(message)) })
  })

  it('handles authenticated blackboard events split across chunks and falls back after a gap', async () => {
    const stream = new ReadableStream<Uint8Array>({ start(controller) {
      for (const chunk of ['event: black', 'board\r\nid: opaque\r\ndata: {"sequence":8}\r\n', '\r\nevent: snapshot_required\ndata: {}\n\n']) {
        controller.enqueue(new TextEncoder().encode(chunk))
      }
      controller.close()
    } })
    const fetchMock = vi.fn().mockResolvedValue(new Response(stream))
    vi.stubGlobal('fetch', fetchMock)
    harnessApi.setApiKey('test-key')
    const onRun = vi.fn()
    const onBoard = vi.fn()
    let close: () => void = () => {}
    const onError = vi.fn(() => close())
    close = harnessApi.watchRun('board', onRun, onError, onBoard, 'cursor+7')
    await vi.waitFor(() => expect(onError).toHaveBeenCalledOnce())
    expect(onBoard).toHaveBeenCalledWith({ sequence: 8 })
    expect(onRun).not.toHaveBeenCalled()
    expect(fetchMock.mock.calls[0]?.[0]).toBe('/v1/runs/board/events?after=cursor%2B7')
  })
})
