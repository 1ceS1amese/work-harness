import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { harnessApi } from '../services/api'
import { fact, hint, objectPage, run, summary } from '../test/blackboard'
import BlackboardRunDetails from './BlackboardRunDetails.vue'
import BlackboardObjectCard from './BlackboardObjectCard.vue'
import RunDetailsDrawer from './RunDetailsDrawer.vue'
import MessageBubble from './MessageBubble.vue'

const stubs = {
  Button: { template: '<button><slot /></button>' },
  Slideover: { template: '<div><slot name="body" /></div>' },
  Alert: { props: ['title'], template: '<p role="alert">{{ title }}</p>' },
  Badge: { template: '<span><slot /></span>' },
  Empty: true,
  Progress: true,
  Input: true,
}
const wrappers: VueWrapper[] = []
beforeEach(() => {
  vi.spyOn(harnessApi, 'blackboard').mockResolvedValue(structuredClone(summary))
  vi.spyOn(harnessApi, 'blackboardObjects').mockResolvedValue(structuredClone(objectPage))
  vi.spyOn(harnessApi, 'getRun').mockResolvedValue(structuredClone(run))
})
afterEach(() => {
  wrappers.splice(0).forEach((wrapper) => wrapper.unmount())
  vi.restoreAllMocks()
})

describe('blackboard details', () => {
  it('separates confirmed scope, directions and hints and exposes remaining acceptance', async () => {
    const wrapper = mount(BlackboardRunDetails, { props: { run }, global: { stubs } })
    wrappers.push(wrapper)
    await flushPromises()
    expect(wrapper.text()).toContain('等待补充')
    expect(wrapper.text()).toContain('Fact · 事实记录')
    expect(wrapper.text()).toContain('Intent · 探索方向')
    expect(wrapper.text()).toContain('Hint · 经验提示')
    expect(wrapper.text()).toContain('仅确认内容已保存')
    expect(wrapper.text()).toContain('经验或假设，尚未核验')
    const criteria = wrapper.findAll('.board-criteria li')
    expect(criteria[0]?.text()).toContain('已满足草稿已保存')
    expect(criteria[1]?.text()).toContain('待满足用户确认适用地区')
    expect(wrapper.text()).toContain('150 Token')
    expect(wrapper.text()).not.toContain('分工数量')
    await wrapper.get('select').setValue('hint')
    await flushPromises()
    expect(harnessApi.blackboardObjects).toHaveBeenLastCalledWith(
      'board',
      { kind: 'hint', at_revision: 7, limit: 20 },
      expect.any(AbortSignal),
    )
  })

  it('opens witness at its exact revision and reads escaped attachment bytes at the same board revision', async () => {
    vi.spyOn(harnessApi, 'blackboardObject').mockResolvedValue({
      board_id: 'board',
      revision: 7,
      item: {
        object: fact,
        event: {
          board_id: 'board',
          sequence: 3,
          source_id: 'source',
          activation_id: null,
          committed_at: summary.evaluated_at,
          changed_refs: [],
          requested_changes: [],
          event_type: 'knowledge',
          finalization: null,
          goal_revision: null,
        },
      },
    })
    const body = '  <script>alert("untrusted")</script>\n草稿\n'
    vi.spyOn(harnessApi, 'blackboardContent').mockResolvedValue({
      board_id: 'board',
      revision: 7,
      content: { ref: fact.attachments[0]!, body },
    })
    const wrapper = mount(BlackboardRunDetails, { props: { run }, global: { stubs } })
    wrappers.push(wrapper)
    await flushPromises()
    await wrapper.get('.board-references button').trigger('click')
    await flushPromises()
    expect(harnessApi.blackboardObject).toHaveBeenCalledWith(
      'board',
      { object_id: fact.id, revision: 1 },
      7,
      expect.any(AbortSignal),
    )
    const selected = wrapper.get('[aria-label="所选依据"]')
    await selected.get('.board-attachments button').trigger('click')
    await flushPromises()
    expect(harnessApi.blackboardContent).toHaveBeenCalledWith(
      'board',
      'b'.repeat(64),
      7,
      expect.any(AbortSignal),
    )
    expect(selected.get('.board-attachments pre').element.textContent).toBe(body)
    expect(wrapper.find('script').exists()).toBe(false)
  })

  it('shows known totals and marks incomplete usage without counting cached input twice', async () => {
    const measured = { ...run, usage: { ...run.usage, cached_tokens: 80, usage_complete: false } }
    vi.mocked(harnessApi.getRun).mockResolvedValue(measured)
    const wrapper = mount(BlackboardRunDetails, { props: { run: measured }, global: { stubs } })
    wrappers.push(wrapper)
    await flushPromises()
    expect(wrapper.text()).toContain('已记录 150 Token')
    expect(wrapper.text()).toContain('输入中缓存命中 80')
    expect(wrapper.text()).toContain('部分调用用量尚未确认')
    expect(wrapper.text()).not.toContain('估算费用')
  })

  it('shows final stop reason, gaps and attempted directions without declaring all work complete', async () => {
    const ended = {
      ...summary,
      finalization: {
        board_id: 'board',
        status: 'partial' as const,
        reason: 'budget',
        finalized_at_revision: 7,
        finalized_at: summary.evaluated_at,
        output: '保留草稿',
        witnesses: summary.goal.witnesses,
        missing_criteria: ['region'],
        attempted_directions: ['比较地区差异'],
      },
    }
    vi.mocked(harnessApi.blackboard).mockResolvedValue(ended)
    const wrapper = mount(BlackboardRunDetails, {
      props: { run: { ...run, status: 'partial' } },
      global: { stubs },
    })
    wrappers.push(wrapper)
    await flushPromises()
    expect(wrapper.text()).toContain('停止原因 · 已达到运行限制')
    expect(wrapper.text()).toContain('结束时尚缺')
    expect(wrapper.text()).toContain('用户确认适用地区')
    expect(wrapper.text()).toContain('已尝试的方向')
    expect(wrapper.text()).not.toContain('都搞定了')
  })

  it('keeps disputed facts and expired hints visibly distinct from valid evidence', () => {
    const wrapper = mount(BlackboardObjectCard, {
      props: {
        object: { ...fact, lifecycle: 'contested', claim_type: 'user_statement' },
        runId: 'board',
        boardRevision: 7,
        evaluatedAt: summary.evaluated_at,
      },
      global: { stubs },
    })
    wrappers.push(wrapper)
    expect(wrapper.text()).toContain('存在争议')
    expect(wrapper.text()).toContain('陈述内容未被独立核验')
    const expired = mount(BlackboardObjectCard, {
      props: {
        object: { ...hint, expires_at: '2026-10-21T00:00:00Z' },
        runId: 'board',
        boardRevision: 7,
        evaluatedAt: summary.evaluated_at,
      },
      global: { stubs },
    })
    wrappers.push(expired)
    expect(expired.text()).toContain('查看时提示已过期')
  })

  it.each(['legacy', undefined] as const)(
    'routes legacy details with engine %s and releases blackboard reads when the drawer closes',
    async (engine) => {
      const wrapper = mount(RunDetailsDrawer, {
        props: { modelValue: true, run: { ...run, engine }, agents: [] },
        global: { stubs },
      })
      wrappers.push(wrapper)
      expect(wrapper.text()).toContain('分工数量')
      expect(harnessApi.blackboard).not.toHaveBeenCalled()
      await wrapper.setProps({ run })
      await flushPromises()
      expect(wrapper.text()).not.toContain('分工数量')
      expect(wrapper.findComponent(BlackboardRunDetails).exists()).toBe(true)
      await wrapper.setProps({ modelValue: false })
      expect(wrapper.findComponent(BlackboardRunDetails).exists()).toBe(false)
    },
  )

  it('does not invent planner messages, progress percentage or boolean approvals for a blackboard run', () => {
    const wrapper = mount(MessageBubble, {
      props: {
        run,
        agents: [],
        message: {
          id: 'message',
          role: 'assistant',
          content: run.output,
          runId: run.run_id,
          createdAt: summary.evaluated_at,
          status: 'waiting_human',
        },
      },
      global: { stubs },
    })
    wrappers.push(wrapper)
    expect(wrapper.text()).toContain('共享黑板')
    expect(wrapper.text()).not.toContain('任务管家')
    expect(wrapper.find('.approval-box').exists()).toBe(false)
    expect(wrapper.find('progress-stub').exists()).toBe(false)
    expect(wrapper.find('.run-facts').exists()).toBe(false)
  })
})
