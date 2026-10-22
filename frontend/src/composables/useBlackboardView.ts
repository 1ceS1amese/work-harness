import { onScopeDispose, ref, shallowRef, watch } from 'vue'

import { ApiError, harnessApi } from '../services/api'
import type { BlackboardObjectPage, BlackboardSummary, ObjectKind, RunResult } from '../types'
import { TERMINAL_RUN_STATUSES } from '../utils/run'

// A view is a pinned snapshot. Notifications never splice newer objects into
// already loaded pages, evidence or goal evaluation.
export function useBlackboardView(run: () => RunResult, onRun: (value: RunResult) => void) {
  const summary = shallowRef<BlackboardSummary | null>(null)
  const page = shallowRef<BlackboardObjectPage | null>(null)
  const kind = ref<ObjectKind | ''>('')
  const loading = ref(false)
  const error = ref('')
  const monitorError = ref('')
  const hasUpdates = ref(false)
  const follow = ref(false)
  const historicalRevision = ref<number | null>(null)
  const latestRevision = ref(0)
  let request: AbortController | undefined
  let monitor: AbortController | undefined
  let timer: ReturnType<typeof setTimeout> | undefined
  let session = 0
  let closeStream: (() => void) | undefined
  let updateTimer: ReturnType<typeof setTimeout> | undefined

  function stopStream(): void {
    closeStream?.()
    closeStream = undefined
    clearTimeout(updateTimer)
  }

  function scheduleUpdate(): void {
    clearTimeout(updateTimer)
    updateTimer = setTimeout(() => {
      if (!follow.value || historicalRevision.value !== null) return
      if (loading.value) scheduleUpdate()
      else void read('refresh')
    }, 150)
  }

  function fail(cause: unknown): void {
    error.value = cause instanceof Error ? cause.message : '黑板读取失败，请重试。'
    if (cause instanceof ApiError && cause.code === 'snapshot_required') {
      summary.value = null
      page.value = null
      hasUpdates.value = true
    }
  }

  async function read(mode: 'refresh' | 'filter' | 'more'): Promise<void> {
    if (mode === 'more' && (!page.value?.next_cursor || loading.value)) return
    request?.abort()
    const current = new AbortController()
    request = current
    loading.value = true
    error.value = ''
    const runId = run().run_id
    try {
      const next = mode === 'refresh' || !summary.value
        ? await harnessApi.blackboard(runId, current.signal, historicalRevision.value ?? undefined)
        : summary.value
      if (current.signal.aborted) return
      const result = await harnessApi.blackboardObjects(
        runId,
        mode === 'more'
          ? { cursor: page.value!.next_cursor!, limit: 20 }
          : { at_revision: next.revision, kind: kind.value || undefined, limit: 20 },
        current.signal,
      )
      if (current.signal.aborted) return
      if (result.board_id !== next.board_id || result.revision !== next.revision) {
        throw new Error('对象与目标的快照版本不一致，请刷新。')
      }
      page.value = mode === 'more' && page.value
        ? { ...result, items: [...page.value.items, ...result.items] }
        : result
      summary.value = next
      if (mode === 'refresh') hasUpdates.value = latestRevision.value > next.revision
    } catch (cause) {
      if (!current.signal.aborted) fail(cause)
    } finally {
      if (!current.signal.aborted) loading.value = false
    }
  }

  async function probe(epoch: number): Promise<void> {
    monitor = new AbortController()
    const current = monitor
    const results = await Promise.allSettled([
      harnessApi.blackboard(run().run_id, current.signal),
      harnessApi.getRun(run().run_id, current.signal),
    ])
    if (session !== epoch || current.signal.aborted) return
    const [board, latest] = results
    const failure = results.find((result) => result.status === 'rejected')
    monitorError.value = failure?.status === 'rejected'
      ? (failure.reason instanceof Error ? failure.reason.message : '更新检查失败，请刷新。') : ''
    if (latest.status === 'fulfilled') onRun(latest.value)
    if (board.status === 'fulfilled') {
      latestRevision.value = board.value.revision
      if (!summary.value && !loading.value) await read('refresh')
      else if (summary.value) {
        hasUpdates.value = board.value.revision !== summary.value.revision
          || JSON.stringify(board.value.goal) !== JSON.stringify(summary.value.goal)
        if (hasUpdates.value && follow.value && historicalRevision.value === null && !loading.value) {
          await read('refresh')
        }
      }
    }
    if (session !== epoch || current.signal.aborted) return
    // Waiting runs may receive input in another client. Check until the board
    // and index both report termination; notifications are only an optimization.
    if (board.status === 'fulfilled' && board.value.finalization
      && latest.status === 'fulfilled' && TERMINAL_RUN_STATUSES.includes(latest.value.status)) return
    if (failure?.status === 'rejected' && failure.reason instanceof ApiError
      && [401, 403, 404].includes(failure.reason.status)) return
    timer = setTimeout(() => { void probe(epoch) }, 3000)
  }

  async function refresh(): Promise<void> {
    const epoch = session
    await read('refresh')
    if (epoch !== session || request?.signal.aborted) return
    clearTimeout(timer)
    monitor?.abort()
    void probe(session)
  }

  watch(() => run().run_id, () => {
    session += 1
    request?.abort()
    monitor?.abort()
    clearTimeout(timer)
    summary.value = null
    page.value = null
    kind.value = ''
    hasUpdates.value = false
    historicalRevision.value = null
    latestRevision.value = 0
    stopStream()
    monitorError.value = ''
    void refresh()
  }, { immediate: true })
  watch(kind, () => {
    page.value = null
    void read('filter')
  })
  watch([follow, historicalRevision, () => summary.value?.board_id, () => Boolean(summary.value?.finalization)], () => {
    stopStream()
    if (!follow.value || historicalRevision.value !== null || !summary.value || summary.value.finalization) return
    if (typeof EventSource === 'undefined' && !harnessApi.hasApiKey()) return
    const runId = run().run_id
    closeStream = harnessApi.watchRun(runId, onRun, stopStream, (event) => {
      if (event.board_id !== runId || event.sequence <= (summary.value?.revision ?? 0)) return
      latestRevision.value = Math.max(latestRevision.value, event.sequence)
      hasUpdates.value = true
      scheduleUpdate()
    }, summary.value.event_cursor)
  })
  onScopeDispose(() => {
    session += 1
    request?.abort()
    monitor?.abort()
    clearTimeout(timer)
    stopStream()
  })

  async function viewRevision(revision: number): Promise<void> {
    follow.value = false
    historicalRevision.value = revision
    await refresh()
  }

  async function showLatest(): Promise<void> {
    historicalRevision.value = null
    await refresh()
    follow.value = true
  }

  return { summary, page, kind, loading, error, monitorError, hasUpdates, follow, historicalRevision,
    refresh, viewRevision, showLatest, loadMore: () => read('more') }
}
