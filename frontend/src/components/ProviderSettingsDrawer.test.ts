import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { harnessApi } from '../services/api'
import ProviderSettingsDrawer from './ProviderSettingsDrawer.vue'

vi.mock('../services/api', () => ({
  harnessApi: {
    providers: vi.fn(),
    saveProvider: vi.fn(),
    deleteProvider: vi.fn(),
  },
}))

const providersMock = vi.mocked(harnessApi.providers)

beforeEach(() => {
  providersMock.mockReset()
  providersMock.mockResolvedValue({
    restart_required: true,
    items: [
      {
        name: 'team-model',
        source: 'persistent',
        loaded: false,
        endpoint_available: true,
        credential_available: false,
        config: {
          adapter: 'openai_compatible',
          protocol_variant: 'openai',
          api_family: 'responses',
          structured_output_mode: null,
          chat_token_parameter: null,
          store_responses: false,
          enabled: true,
          base_url: 'https://models.example.test/v1',
          base_url_env: null,
          api_key_env: 'TEAM_MODEL_API_KEY',
          allowed_models: ['main-model', 'worker-model'],
          capabilities: ['chat', 'structured_output', 'tool_calling'],
          timeout_seconds: 60,
          allow_insecure_http: false,
          allow_private_network: false,
          options: {},
        },
        model_bindings: { 'worker-fast': 'worker-model' },
        agent_bindings: { main: 'main-model', 'worker-02': 'worker-model' },
      },
    ],
  })
})

describe('ProviderSettingsDrawer', () => {
  it('loads persisted providers and exposes restart and route state', async () => {
    const wrapper = mount(ProviderSettingsDrawer, {
      props: {
        modelValue: true,
        logicalModels: ['planner-strong', 'worker-fast'],
        agents: [
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
      global: {
        stubs: {
          ElDrawer: {
            props: ['modelValue'],
            template: '<div><slot name="header" /><slot /></div>',
          },
          ElAlert: {
            props: ['title', 'description'],
            template: '<div>{{ title }} {{ description }}</div>',
          },
          ElTag: { template: '<span><slot /></span>' },
          ElButton: { template: '<button><slot /></button>' },
          ElPopconfirm: { template: '<div><slot name="reference" /></div>' },
          ElSkeleton: true,
          ElEmpty: true,
          ElForm: { template: '<form><slot /></form>' },
          ElFormItem: { template: '<label><slot /></label>' },
          ElInput: true,
          ElInputNumber: true,
          ElSwitch: true,
          ElSelect: { template: '<select><slot /></select>' },
          ElOption: true,
          ElCheckbox: { template: '<label><slot /></label>' },
        },
      },
    })

    await flushPromises()

    expect(providersMock).toHaveBeenCalledOnce()
    expect(wrapper.text()).toContain('已保存的配置尚未装载')
    expect(wrapper.text()).toContain('team-model')
    expect(wrapper.text()).toContain('worker-fast → worker-model')
    expect(wrapper.text()).toContain('worker-02 → worker-model')
    expect(wrapper.text()).toContain('TEAM_MODEL_API_KEY')
  })
})
