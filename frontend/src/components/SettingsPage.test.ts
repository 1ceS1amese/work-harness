import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import SettingsPage from './SettingsPage.vue'

describe('SettingsPage', () => {
  it('provides independent model, prompt, and runtime sections', async () => {
    const wrapper = mount(SettingsPage, {
      props: {
        logicalModels: ['planner-strong'],
        agents: [],
        connectionLabel: '等待配置模型服务',
      },
      global: {
        stubs: {
          Button: { template: '<button><slot /></button>' },
          ProviderSettingsPanel: { template: '<div>模型设置内容</div>' },
          PromptSettingsPanel: { template: '<div>提示词设置内容</div>' },
          RuntimeSettingsPanel: { template: '<div>运行配置内容</div>' },
        },
      },
    })

    expect(wrapper.get('.active-title').text()).toBe('模型连接')
    expect(wrapper.text()).toContain('模型设置')
    expect(wrapper.text()).toContain('Agent 提示词')
    expect(wrapper.text()).toContain('运行配置')
    expect(wrapper.text()).toContain('模型设置内容')

    const navigation = wrapper.find('nav[aria-label="设置分类"]')
    await navigation.findAll('button')[1]!.trigger('click')
    expect(wrapper.text()).toContain('提示词设置内容')
    expect(wrapper.emitted('sectionChange')?.slice(-1)[0]).toEqual(['prompts'])

    await navigation.findAll('button')[2]!.trigger('click')
    expect(wrapper.text()).toContain('运行配置内容')
    expect(wrapper.emitted('sectionChange')?.slice(-1)[0]).toEqual(['runtime'])

    await wrapper.get('button[aria-label="打开工作空间设置"]').trigger('click')
    expect(wrapper.text()).toContain('模型设置内容')
    expect(wrapper.emitted('sectionChange')?.slice(-1)[0]).toEqual(['models'])
    expect(wrapper.emitted('authenticate')).toBeUndefined()
  })
})
