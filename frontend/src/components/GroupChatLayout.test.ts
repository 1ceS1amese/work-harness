import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import GroupChatLayout from './GroupChatLayout.vue'

describe('群聊编排模板', () => {
  it('统一渲染中文群名、成员和业务插槽', () => {
    const wrapper = mount(GroupChatLayout, {
      props: {
        mobileMenuOpen: false,
        title: '项目交付协作群',
        connectionLabel: '系统就绪',
        connectionState: 'online',
        members: [
          {
            id: 'worker-01',
            display_name: '智能成员一',
            responsibility: '执行任务',
            specialty: '按任务选择功能',
            assignable: true,
            model_route: null,
          },
          {
            id: 'worker-02',
            display_name: '智能成员二',
            responsibility: '执行任务',
            specialty: '按任务选择功能',
            assignable: true,
            model_route: null,
          },
        ],
      },
      slots: {
        sidebar: '<aside>群聊列表</aside>',
        conversation: '<section>群消息记录</section>',
        composer: '<footer>任务输入区</footer>',
      },
      global: {
        stubs: {
          ElButton: { template: '<button><slot /></button>' },
          ElDrawer: { template: '<div><slot /></div>' },
        },
      },
    })

    expect(wrapper.text()).toContain('项目交付协作群')
    expect(wrapper.find('[title="智能成员一"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('2 位 AI 小伙伴在群里')
    expect(wrapper.text()).toContain('群公告')
    expect(wrapper.text()).toContain('你群主')
    expect(wrapper.text()).toContain('群聊列表')
    expect(wrapper.text()).toContain('群消息记录')
    expect(wrapper.text()).toContain('任务输入区')
  })
})
