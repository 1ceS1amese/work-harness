import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import App from './App.vue'
import { deliverableTemplates } from './test/deliverables'
import type { CapabilitiesResponse, WorkflowCapabilities } from './types'

const mocked = vi.hoisted(() => ({
  health: vi.fn(),
  capabilities: vi.fn(),
  createRun: vi.fn(),
  watchRun: vi.fn(),
  warning: vi.fn(),
}))
vi.mock('./services/api', async (original) => {
  const actual = await original<typeof import('./services/api')>()
  return { ...actual, harnessApi: { ...actual.harnessApi, ...mocked } }
})
vi.mock('./composables/useNotice', () => ({
  useNotice: () => ({ warning: mocked.warning, success: vi.fn(), error: vi.fn() }),
}))

const workflow: WorkflowCapabilities = {
  default_engine: 'blackboard',
  accept_new_blackboard_runs: true,
  blackboard_templates: deliverableTemplates,
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
  if (scrollToDescriptor)
    Object.defineProperty(HTMLElement.prototype, 'scrollTo', scrollToDescriptor)
  else Reflect.deleteProperty(HTMLElement.prototype, 'scrollTo')
})

async function render(policy = workflow) {
  mocked.capabilities.mockResolvedValue({
    workflow: policy,
    task_types: ['general'],
    logical_models: [],
    agents: [],
    skills: [],
  } satisfies Partial<CapabilitiesResponse>)
  wrapper = mount(App, {
    global: {
      stubs: {
        ChatSidebar: true,
        RunHistoryDrawer: true,
        RunDetailsDrawer: true,
        MessageBubble: true,
        Popover: { template: '<div><slot /><slot name="content" /></div>' },
        Slideover: true,
        Modal: true,
        Switch: true,
        Button: { template: '<button><slot /></button>' },
        Badge: { template: '<span><slot /></span>' },
        Input: true,
      },
    },
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
  it.each(deliverableTemplates)('submits the exact criteria for $title', async (template) => {
    const view = await render()
    expect(view.get('select[aria-label="协作方式"]').text()).toContain('跟随项目设置（黑板协作）')
    await send(view)
    expect(mocked.createRun).not.toHaveBeenCalled()
    expect(mocked.warning).toHaveBeenCalledWith('请先选择黑板任务的完成条件。')
    const select = view.get('select[aria-label="黑板任务完成条件"]')
    expect(select.findAll('option').map((option) => option.text())).toEqual([
      '请选择交付物',
      '分析报告',
      '实施方案',
      '操作清单',
    ])
    await select.setValue(template.id)
    expect(view.find('.completion-note').exists()).toBe(false)
    await send(view)
    expect(mocked.createRun).toHaveBeenCalledWith(
      expect.objectContaining({
        goal: '起草项目说明',
        engine: 'blackboard',
        blackboard: template.input,
        assignment: expect.objectContaining({ cross_review: false }),
      }),
    )
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
