import { afterEach, describe, expect, it, vi } from 'vitest'

import { harnessApi } from './api'

afterEach(() => {
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
})
