import { effectScope, ref } from 'vue'
import { flushPromises } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError, harnessApi } from '../services/api'
import { fact, hint, objectPage, run, summary } from '../test/blackboard'
import { useBlackboardView } from './useBlackboardView'

const scopes: ReturnType<typeof effectScope>[] = []
function setup() {
  const current = ref(structuredClone(run))
  const onRun = vi.fn()
  const scope = effectScope()
  scopes.push(scope)
  const view = scope.run(() => useBlackboardView(() => current.value, onRun))!
  return { current, onRun, scope, ...view }
}

beforeEach(() => {
  vi.useFakeTimers()
  vi.spyOn(harnessApi, 'blackboard').mockResolvedValue(structuredClone(summary))
  vi.spyOn(harnessApi, 'blackboardObjects').mockResolvedValue(structuredClone(objectPage))
  vi.spyOn(harnessApi, 'getRun').mockResolvedValue(structuredClone(run))
})
afterEach(() => {
  scopes.splice(0).forEach(scope => scope.stop())
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

describe('pinned blackboard view', () => {
  it('follows committed events from the snapshot cursor and stops following on historical navigation', async () => {
    vi.stubGlobal('EventSource', class {})
    const close = vi.fn()
    const stream = vi.spyOn(harnessApi, 'watchRun').mockReturnValue(close)
    const view = setup()
    await flushPromises()
    view.follow.value = true
    await flushPromises()
    expect(stream.mock.calls[0]?.[4]).toBe(summary.event_cursor)
    vi.mocked(harnessApi.blackboard).mockResolvedValue({ ...summary, revision: 8 })
    vi.mocked(harnessApi.blackboardObjects).mockResolvedValue({ ...objectPage, revision: 8 })
    stream.mock.calls[0]?.[3]?.({ board_id: 'board', sequence: 8 } as never)
    await vi.advanceTimersByTimeAsync(150)
    expect(view.summary.value?.revision).toBe(8)
    vi.mocked(harnessApi.blackboard).mockImplementation(async (_id, _signal, revision) => ({ ...summary, revision: revision ?? 9 }))
    vi.mocked(harnessApi.blackboardObjects).mockResolvedValue({ ...objectPage, revision: 3 })
    await view.viewRevision(3)
    expect(view.follow.value).toBe(false)
    expect(close).toHaveBeenCalled()
    expect(view.summary.value?.revision).toBe(3)
    await vi.advanceTimersByTimeAsync(3000)
    expect(view.summary.value?.revision).toBe(3)
    expect(view.hasUpdates.value).toBe(true)
    vi.mocked(harnessApi.blackboardObjects).mockResolvedValue({ ...objectPage, revision: 9 })
    await view.showLatest()
    expect(view.summary.value?.revision).toBe(9)
    expect(view.follow.value).toBe(true)
  })

  it('refreshes through polling after an SSE connection drops without mixing pages', async () => {
    vi.stubGlobal('EventSource', class {})
    const close = vi.fn()
    const stream = vi.spyOn(harnessApi, 'watchRun').mockReturnValue(close)
    const view = setup()
    await flushPromises()
    view.follow.value = true
    await flushPromises()
    stream.mock.calls[0]?.[2]()
    expect(close).toHaveBeenCalled()
    vi.mocked(harnessApi.blackboard).mockResolvedValue({ ...summary, revision: 9 })
    vi.mocked(harnessApi.blackboardObjects).mockResolvedValue({ ...objectPage, revision: 9 })
    await vi.advanceTimersByTimeAsync(3000)
    expect(view.summary.value?.revision).toBe(9)
    expect(view.page.value?.revision).toBe(9)
  })
  it('keeps goal, pagination and filters on the viewed revision while newer commits arrive', async () => {
    vi.mocked(harnessApi.blackboardObjects).mockResolvedValueOnce({ ...objectPage, items: [fact], next_cursor: 'page-7-fact' })
    const view = setup()
    await flushPromises()
    vi.mocked(harnessApi.blackboard).mockResolvedValue({ ...summary, revision: 8, goal: { ...summary.goal, missing_criteria: [] } })
    await vi.advanceTimersByTimeAsync(3000)
    expect(view.hasUpdates.value).toBe(true)
    expect(view.summary.value?.revision).toBe(7)
    expect(view.summary.value?.goal.missing_criteria).toEqual(['region'])
    vi.mocked(harnessApi.blackboardObjects).mockResolvedValueOnce({ ...objectPage, items: [hint] })
    await view.loadMore()
    expect(harnessApi.blackboardObjects).toHaveBeenLastCalledWith('board', { cursor: 'page-7-fact', limit: 20 }, expect.any(AbortSignal))
    expect(view.page.value?.items.map(item => item.id)).toEqual([fact.id, hint.id])
    view.kind.value = 'hint'
    await flushPromises()
    expect(harnessApi.blackboardObjects).toHaveBeenLastCalledWith('board', { at_revision: 7, kind: 'hint', limit: 20 }, expect.any(AbortSignal))
    vi.mocked(harnessApi.blackboardObjects).mockResolvedValue({ ...objectPage, revision: 8 })
    await view.refresh()
    expect(view.summary.value?.revision).toBe(8)
    expect(view.hasUpdates.value).toBe(false)
  })

  it('ignores late pages after changing filters, including transports that ignore abort', async () => {
    const view = setup()
    await flushPromises()
    let complete!: (value: typeof objectPage) => void
    vi.mocked(harnessApi.blackboardObjects).mockImplementationOnce(() => new Promise(resolve => { complete = resolve }))
    view.kind.value = 'fact'
    await flushPromises()
    vi.mocked(harnessApi.blackboardObjects).mockResolvedValueOnce({ ...objectPage, items: [hint] })
    view.kind.value = 'hint'
    await flushPromises()
    complete({ ...objectPage, items: [fact] })
    await flushPromises()
    expect(view.page.value?.items).toEqual([hint])
    expect(view.loading.value).toBe(false)
  })

  it('clears unavailable history instead of mixing it with a fresh snapshot', async () => {
    const view = setup()
    await flushPromises()
    vi.mocked(harnessApi.blackboardObjects).mockRejectedValueOnce(new ApiError('请刷新完整黑板', 410, 'snapshot_required'))
    view.kind.value = 'fact'
    await flushPromises()
    expect(view.summary.value).toBeNull()
    expect(view.page.value).toBeNull()
    expect(view.error.value).toContain('请刷新完整黑板')
    await view.refresh()
    expect(view.summary.value?.revision).toBe(7)
  })

  it('waits for initialization and stops timers and requests when disposed', async () => {
    vi.mocked(harnessApi.blackboard).mockRejectedValueOnce(new ApiError('正在初始化', 409, 'blackboard_not_initialized'))
    const view = setup()
    await flushPromises()
    expect(view.summary.value?.revision).toBe(7)
    expect(view.onRun).toHaveBeenCalledWith(run)
    view.scope.stop()
    const count = vi.mocked(harnessApi.blackboard).mock.calls.length
    await vi.advanceTimersByTimeAsync(10000)
    expect(harnessApi.blackboard).toHaveBeenCalledTimes(count)
    expect(vi.mocked(harnessApi.blackboard).mock.calls[count - 1]?.[1]?.aborted).toBe(true)
  })

  it('rejects a mismatched page without replacing the coherent snapshot', async () => {
    const view = setup()
    await flushPromises()
    vi.mocked(harnessApi.blackboardObjects).mockResolvedValueOnce({ ...objectPage, revision: 999 })
    await view.refresh()
    expect(view.error.value).toContain('版本不一致')
    expect(view.summary.value?.revision).toBe(7)
    expect(view.page.value?.revision).toBe(7)
  })

  it('does not let a previous run response replace the selected run', async () => {
    let complete!: (value: typeof summary) => void
    vi.mocked(harnessApi.blackboard).mockImplementationOnce(() => new Promise(resolve => { complete = resolve }))
    const view = setup()
    vi.mocked(harnessApi.blackboard).mockResolvedValue({ ...summary, board_id: 'second' })
    vi.mocked(harnessApi.blackboardObjects).mockResolvedValue({ ...objectPage, board_id: 'second' })
    view.current.value = { ...run, run_id: 'second' }
    await flushPromises()
    complete(summary)
    await flushPromises()
    expect(view.summary.value?.board_id).toBe('second')
  })
})
