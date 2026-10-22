import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import App from './App.vue'
import { intent } from './test/blackboard'
import type { CapabilitiesResponse, WorkflowCapabilities } from './types'

const mocked = vi.hoisted(() => ({
  health: vi.fn(), capabilities: vi.fn(), createRun: vi.fn(), watchRun: vi.fn(), warning: vi.fn(),
}))
vi.mock('./services/api', async (original) => {
  const actual = await original<typeof import('./services/api')>()
  return { ...actual, harnessApi: { ...actual.harnessApi, ...mocked } }
})
vi.mock('element-plus', () => ({
  ElMessage: { warning: mocked.warning, success: vi.fn(), error: vi.fn() },
}))

const workflow: WorkflowCapabilities = {
  default_engine: 'blackboard', accept_new_blackboard_runs: true,
  blackboard_templates: [{
    id: 'saved-draft', title: '保存候选草稿', description: '草稿已保存；内容准确性仍需核验。',
    input: { topic: 'document', acceptance: [intent.acceptance[0]!] },
  }],
}

let wrapper: VueWrapper | undefined
const scrollToDescriptor = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'scrollTo')
beforeEach(() => {
  localStorage.clear()
  window.history.replaceState({}, '', '/')
  vi.clearAllMocks()
  vi.stubGlobal('EventSource', class {})
  Object.defineProperty(HTMLElement.prototype, 'scrollTo', { value: vi.fn(), configurable: true })
  mocked.health.mockResolvedValue({ status: 'ok' })
  mocked.createRun.mockResolvedValue({ run_id: 'new-run', status_url: '/v1/runs/new-run' })
  mocked.watchRun.mockReturnValue(() => {})
})
afterEach(() => {
  wrapper?.unmount()
  wrapper = undefined
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  if (scrollToDescriptor) Object.defineProperty(HTMLElement.prototype, 'scrollTo', scrollToDescriptor)
  else Reflect.deleteProperty(HTMLElement.prototype, 'scrollTo')
})

async function render(policy = workflow) {
  mocked.capabilities.mockResolvedValue({
    workflow: policy, task_types: ['general'], logical_models: [], agents: [], skills: [],
  } satisfies Partial<CapabilitiesResponse>)
  wrapper = mount(App, {
    global: { stubs: {
      ChatSidebar: true, RunHistoryDrawer: true, RunDetailsDrawer: true, MessageBubble: true,
      ElPopover: { template: '<div><slot name="reference" /></div>' },
      ElDrawer: true, ElDialog: true, ElSwitch: true,
      ElSelect: { template: '<select><slot /></select>' }, ElOption: { template: '<option><slot /></option>' },
      ElButton: { template: '<button><slot /></button>' }, ElTag: { template: '<span><slot /></span>' },
      ElInput: true,
    } },
  })
  await flushPromises()
  return wrapper
}

async function send(view: VueWrapper, goal = '起草项目说明') {
  await view.get('textarea[aria-label="任务目标"]').setValue(goal)
  await view.get('button[aria-label="发送任务"]').trigger('click')
  await flushPromises()
}

describe('chat blackboard admission', () => {
  it('requires an explicit completion choice and sends those exact criteria', async () => {
    const view = await render()
    expect(view.get('select[aria-label="协作方式"]').text()).toContain('跟随项目设置（黑板协作）')
    await send(view)
    expect(mocked.createRun).not.toHaveBeenCalled()
    expect(mocked.warning).toHaveBeenCalledWith('请先选择黑板任务的完成条件。')
    await view.get('select[aria-label="黑板任务完成条件"]').setValue('saved-draft')
    expect(view.text()).toContain('内容准确性仍需核验')
    await send(view)
    expect(mocked.createRun).toHaveBeenCalledWith(expect.objectContaining({
      goal: '起草项目说明', engine: 'blackboard', blackboard: workflow.blackboard_templates[0]!.input,
      assignment: expect.objectContaining({ cross_review: false }),
    }))
  })

  it('keeps legacy admission usable when the project defaults to legacy', async () => {
    const view = await render({ ...workflow, default_engine: 'legacy' })
    expect(view.find('select[aria-label="黑板任务完成条件"]').exists()).toBe(false)
    await send(view)
    expect(mocked.createRun).toHaveBeenCalledWith(expect.objectContaining({ engine: 'legacy' }))
    expect(mocked.createRun.mock.calls[0]![0]).not.toHaveProperty('blackboard')
  })

  it('blocks new blackboard requests after rollback and allows an explicit legacy choice', async () => {
    const view = await render({ ...workflow, accept_new_blackboard_runs: false })
    expect(view.text()).toContain('当前已停止接收新的黑板任务')
    await send(view)
    expect(mocked.createRun).not.toHaveBeenCalled()
    await view.get('select[aria-label="协作方式"]').setValue('legacy')
    await send(view)
    expect(mocked.createRun).toHaveBeenCalledWith(expect.objectContaining({ engine: 'legacy' }))
  })
})
