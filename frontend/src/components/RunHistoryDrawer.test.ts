import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError, harnessApi } from '../services/api'
import { summary } from '../test/blackboard'
import RunHistoryDrawer from './RunHistoryDrawer.vue'

const stubs = {
  ElDrawer: { template: '<div><slot /></div>' },
  ElButton: { template: '<button><slot /></button>' },
  ElAlert: { props: ['title'], template: '<p role="alert">{{ title }}</p>' },
}
afterEach(() => vi.restoreAllMocks())

describe('service run history', () => {
  it('opens API-created blackboard and legacy runs without requiring local conversation messages', async () => {
    const item = { run_id: 'api-board', status: 'waiting_human' as const, engine: 'blackboard' as const,
      created_at: summary.evaluated_at, updated_at: summary.evaluated_at, execution: null }
    vi.spyOn(harnessApi, 'listRuns').mockResolvedValue({ items: [item, { ...item, run_id: 'legacy-run', engine: 'legacy' }], offset: 0, limit: 20 })
    const wrapper = mount(RunHistoryDrawer, { props: { modelValue: true }, global: { stubs } })
    await flushPromises()
    expect(wrapper.text()).toContain('共享黑板')
    expect(wrapper.text()).toContain('等待补充')
    expect(wrapper.text()).toContain('任务协作')
    await wrapper.findAll('button').find(button => button.text() === '查看运行详情')!.trigger('click')
    expect(wrapper.emitted('select')).toEqual([['api-board']])
    await wrapper.setProps({ modelValue: false })
    expect(vi.mocked(harnessApi.listRuns).mock.calls[0]?.[1]?.aborted).toBe(true)
    wrapper.unmount()
  })

  it('exposes authentication failures with a retry path', async () => {
    vi.spyOn(harnessApi, 'listRuns').mockRejectedValueOnce(new ApiError('需要部署身份验证', 401))
      .mockResolvedValue({ items: [], limit: 20, offset: 0 })
    const wrapper = mount(RunHistoryDrawer, { props: { modelValue: true }, global: { stubs } })
    await flushPromises()
    expect(wrapper.get('[role="alert"]').text()).toBe('需要部署身份验证')
    await wrapper.findAll('button').find(button => button.text() === '刷新')!.trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('当前页没有运行记录')
    wrapper.unmount()
  })
})
