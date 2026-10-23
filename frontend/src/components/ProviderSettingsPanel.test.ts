import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { harnessApi } from '../services/api'
import ProviderSettingsPanel from './ProviderSettingsPanel.vue'

vi.mock('../services/api', () => ({
  harnessApi: {
    providers: vi.fn(),
    saveProvider: vi.fn(),
    deleteProvider: vi.fn(),
  },
}))

const providersMock = vi.mocked(harnessApi.providers)
const uiStubs = {
  Alert: {
    props: ['title', 'description'],
    template: '<div>{{ title }} {{ description }}</div>',
  },
  Badge: { template: '<span><slot /></span>' },
  Button: { template: '<button><slot /></button>' },
  ConfirmAction: { template: '<div><slot name="reference" /></div>' },
  Skeleton: true,
  Empty: true,
  FormField: { template: '<label><slot /></label>' },
  Input: true,
  InputNumber: true,
  Switch: true,
  Checkbox: { template: '<label><slot /></label>' },
}

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
          credential_source: 'environment',
          api_key_env: 'TEAM_MODEL_API_KEY',
          allowed_models: ['main-model', 'worker-model'],
          capabilities: ['chat', 'structured_output', 'tool_calling'],
          timeout_seconds: 60,
          max_concurrency: 8,
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

describe('ProviderSettingsPanel', () => {
  it('loads persisted providers and exposes restart and route state', async () => {
    const wrapper = mount(ProviderSettingsPanel, {
      props: {
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
        stubs: uiStubs,
      },
    })

    await flushPromises()

    expect(providersMock).toHaveBeenCalledOnce()
    expect(wrapper.text()).toContain('新设置正在等待重启')
    expect(wrapper.text()).toContain('team-model')
    expect(wrapper.text()).toContain('页面保存')
    expect(wrapper.text()).toContain('OpenAI Responses')
    expect(wrapper.text()).toContain('动手处理（快速） → worker-model')
    expect(wrapper.text()).toContain('智能成员二 → worker-model')
    expect(wrapper.text()).not.toContain('persistent')
    expect(wrapper.text()).not.toContain('openai_compatible')
    expect(wrapper.text()).not.toContain('TEAM_MODEL_API_KEY')
  })

  it('opens a friendly first-run form instead of exposing storage internals', async () => {
    const templateProvider = (await providersMock()).items[0]!
    providersMock.mockClear()
    providersMock.mockResolvedValueOnce({
      restart_required: false,
      items: [
        {
          ...templateProvider,
          name: 'compatible',
          source: 'yaml',
          loaded: true,
          endpoint_available: false,
          credential_available: false,
        },
      ],
    })
    const wrapper = mount(ProviderSettingsPanel, {
      props: {
        setupMode: true,
        logicalModels: ['planner-strong', 'worker-fast'],
        agents: [],
      },
      global: { stubs: uiStubs },
    })

    await flushPromises()

    expect(wrapper.text()).toContain('完成首次模型配置')
    expect(wrapper.text()).toContain('连接真实模型后才能开始任务')
    expect(wrapper.text()).toContain('编辑默认模型服务')
    expect(wrapper.text()).toContain('模型选择')
    expect(wrapper.text()).not.toContain('YAML')
    expect(wrapper.text()).not.toContain('SQLite')
  })
})
