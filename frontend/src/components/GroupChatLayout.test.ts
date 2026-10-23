import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import GroupChatLayout from './GroupChatLayout.vue'

describe('workspace layout', () => {
  it('keeps task navigation, connection state and working content in dashboard slots', async () => {
    const wrapper = mount(GroupChatLayout, {
      props: {
        mobileMenuOpen: false,
        title: '项目交付计划',
        connectionLabel: '系统就绪',
        connectionState: 'online',
        empty: false,
      },
      slots: {
        sidebar: '<nav>会话列表</nav>',
        conversation: '<section>任务消息</section>',
        composer: '<footer>目标输入</footer>',
      },
    })
    expect(wrapper.get('.active-title').text()).toBe('项目交付计划')
    expect(wrapper.text()).toContain('系统就绪')
    expect(wrapper.text()).toContain('会话列表')
    expect(wrapper.text()).toContain('任务消息')
    expect(wrapper.text()).toContain('目标输入')
    expect(wrapper.find('.group-info-panel').exists()).toBe(false)
    await wrapper.get('button[aria-label="打开工作空间设置"]').trigger('click')
    expect(wrapper.emitted('settings')).toHaveLength(1)
    expect(wrapper.emitted('authenticate')).toBeUndefined()
  })
})
