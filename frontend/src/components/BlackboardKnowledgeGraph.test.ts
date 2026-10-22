import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { harnessApi } from '../services/api'
import { fact, hint, intent, objectPage, summary } from '../test/blackboard'
import type { BlackboardObjectDetail, BoardEvent } from '../types'
import BlackboardKnowledgeGraph from './BlackboardKnowledgeGraph.vue'
import BlackboardNodeDetails from './BlackboardNodeDetails.vue'

const event: BoardEvent = { board_id: 'board', sequence: 3, source_id: 'explorer', activation_id: 'activation-1',
  committed_at: summary.evaluated_at, changed_refs: [], requested_changes: [], event_type: 'knowledge', finalization: null, goal_revision: null }
const oldDetail: BlackboardObjectDetail = { board_id: 'board', revision: 7, item: { object: hint, event },
  activation: { activation_id: 'activation-1', source_id: 'explorer', source_version: '1', reason: '地区尚未确认，需要探索',
    intent_ref: { object_id: intent.id, revision: 1 }, input_refs: [{ object_id: intent.id, revision: 1 }] } }
const latestHint = { ...hint, revision: 2, suggestion: '已经比较过地区差异' }
const derivedFact = { ...fact, derived_from: [{ object_id: hint.id, revision: 1 }] }
const stubs = { ElButton: { template: '<button><slot /></button>' } }
const wrappers: VueWrapper[] = []

beforeEach(() => {
  vi.spyOn(harnessApi, 'blackboardObjects').mockResolvedValue({ ...objectPage, items: [derivedFact, latestHint, intent] })
  vi.spyOn(harnessApi, 'blackboardObject').mockResolvedValue(oldDetail)
  vi.spyOn(harnessApi, 'blackboardHistory').mockResolvedValue({ board_id: 'board', revision: 7, object_id: hint.id,
    items: [oldDetail.item, { object: latestHint, event: { ...event, sequence: 5 } }], total: 2, limit: 20, next_cursor: null })
})
afterEach(() => { wrappers.splice(0).forEach(wrapper => wrapper.unmount()); vi.restoreAllMocks() })

describe('interactive knowledge graph', () => {
  it('expands an exact historical reference, shows its activation reason and locates the committed board revision', async () => {
    const wrapper = mount(BlackboardKnowledgeGraph, { props: { runId: 'board', snapshot: summary }, global: { stubs } })
    wrappers.push(wrapper)
    await flushPromises()
    const old = wrapper.get(`g[data-object-id="${hint.id}"][data-object-revision="1"]`)
    expect(old.classes()).toContain('unresolved')
    expect(wrapper.find(`g[data-object-id="${hint.id}"][data-object-revision="2"]`).exists()).toBe(true)
    await old.trigger('keydown', { key: 'Enter' })
    await flushPromises()
    expect(wrapper.emitted('pause')).toBeTruthy()
    expect(harnessApi.blackboardObject).toHaveBeenCalledWith('board', { object_id: hint.id, revision: 1 }, 7, expect.any(AbortSignal))
    expect(wrapper.get(`g[data-object-id="${hint.id}"][data-object-revision="1"]`).classes()).not.toContain('unresolved')
    expect(wrapper.get('[aria-label="知识节点详情"]').text()).toContain('地区尚未确认，需要探索')
    expect(wrapper.get('.graph-connected').text()).toContain('产生来源')
    await wrapper.findAll('button').find(button => button.text() === '定位到当时的板面')!.trigger('click')
    expect(wrapper.emitted('locate')).toEqual([[3, { object_id: hint.id, revision: 1 }]])
    await wrapper.get('input[type="search"]').setValue('已经比较过')
    expect(wrapper.get('.graph-search-results').text()).toContain('找到 1 个节点')
  })

  it('clears old nodes while a new complete snapshot is loading, then reveals a new branch', async () => {
    const wrapper = mount(BlackboardKnowledgeGraph, { props: { runId: 'board', snapshot: summary }, global: { stubs } })
    wrappers.push(wrapper)
    await flushPromises()
    let complete!: (value: typeof objectPage) => void
    vi.mocked(harnessApi.blackboardObjects).mockImplementationOnce(() => new Promise(resolve => { complete = resolve }))
    await wrapper.setProps({ snapshot: { ...summary, revision: 8, counts: { fact: 1, intent: 2, hint: 1 }, objects_cursor: 'objects-8' } })
    expect(wrapper.find('svg').exists()).toBe(false)
    const branch = { ...intent, id: 'new-branch', objective: '核对新的地区约束', parent_intent_ref: { object_id: intent.id, revision: 1 } }
    complete({ ...objectPage, revision: 8, total: 4, items: [fact, hint, intent, branch] })
    await flushPromises()
    expect(wrapper.find('g[data-object-id="new-branch"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('黑板第 8 版')
  })

  it('ignores a late node response and keeps the new selection and history bound to one board', async () => {
    let complete!: (value: BlackboardObjectDetail) => void
    vi.mocked(harnessApi.blackboardObject).mockImplementationOnce(() => new Promise(resolve => { complete = resolve }))
      .mockResolvedValueOnce({ ...oldDetail, item: { object: fact, event } })
    const wrapper = mount(BlackboardNodeDetails, { props: { runId: 'board', snapshot: summary, selected: { object_id: hint.id, revision: 1 } }, global: { stubs } })
    wrappers.push(wrapper)
    await wrapper.setProps({ selected: { object_id: fact.id, revision: 1 } })
    await flushPromises()
    complete(oldDetail)
    await flushPromises()
    expect(wrapper.get('.board-object').text()).toContain(fact.statement)
    expect(wrapper.get('.board-object').text()).not.toContain(hint.suggestion)
    expect(harnessApi.blackboardHistory).toHaveBeenLastCalledWith('board', fact.id, { at_revision: 7, limit: 20 }, expect.any(AbortSignal))
  })

  it('keeps history pagination pinned and exposes unavailable versions instead of substituting latest content', async () => {
    vi.mocked(harnessApi.blackboardHistory).mockResolvedValueOnce({ board_id: 'board', revision: 7, object_id: hint.id,
      items: [oldDetail.item], total: 2, limit: 1, next_cursor: 'history-7-1' })
      .mockResolvedValueOnce({ board_id: 'board', revision: 7, object_id: hint.id,
        items: [{ object: latestHint, event: { ...event, sequence: 5 } }], total: 2, limit: 1, next_cursor: null })
    const wrapper = mount(BlackboardNodeDetails, { props: { runId: 'board', snapshot: summary, selected: { object_id: hint.id, revision: 1 } }, global: { stubs } })
    wrappers.push(wrapper)
    await flushPromises()
    await wrapper.findAll('button').find(button => button.text() === '加载更多历史')!.trigger('click')
    await flushPromises()
    expect(wrapper.findAll('.graph-history li')).toHaveLength(2)
    expect(harnessApi.blackboardHistory).toHaveBeenLastCalledWith('board', hint.id, { cursor: 'history-7-1', limit: 20 }, expect.any(AbortSignal))
    vi.mocked(harnessApi.blackboardObject).mockRejectedValueOnce(new Error('当前快照已不可用，请刷新完整黑板。'))
    await wrapper.setProps({ selected: { object_id: hint.id, revision: 2 } })
    await flushPromises()
    expect(wrapper.find('.board-object').exists()).toBe(false)
    expect(wrapper.get('[role="alert"]').text()).toContain('当前快照已不可用')
  })
})
