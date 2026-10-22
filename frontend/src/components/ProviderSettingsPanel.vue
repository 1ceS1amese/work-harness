<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { CircleCheck, Delete, EditPen, Plus, Refresh } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'

import { harnessApi } from '../services/api'
import { logicalModelLabel } from '../utils/labels'
import type {
  AgentDescriptor,
  ProviderApiFamily,
  ProviderCredentialSource,
  ProviderDescriptor,
  ProviderProfileSpec,
  ProviderProtocolVariant,
} from '../types'

type StructuredOutputMode = '' | 'json_schema' | 'json_object'
type ChatTokenParameter = '' | 'max_completion_tokens' | 'max_tokens'

interface ProviderFormState {
  name: string
  protocolVariant: ProviderProtocolVariant
  apiFamily: ProviderApiFamily
  enabled: boolean
  baseUrl: string
  baseUrlEnv: string
  credentialSource: ProviderCredentialSource
  apiKeyEnv: string
  apiKey: string
  credentialConfigured: boolean
  clearApiKey: boolean
  allowedModels: string[]
  structuredOutputMode: StructuredOutputMode
  chatTokenParameter: ChatTokenParameter
  storeResponses: boolean
  timeoutSeconds: number
  maxConcurrency: number
  allowInsecureHttp: boolean
  allowPrivateNetwork: boolean
  options: Record<string, unknown>
  defaultModel: string
  modelBindings: Record<string, string>
  agentBindings: Record<string, string>
}

const props = withDefaults(defineProps<{
  logicalModels: string[]
  agents: AgentDescriptor[]
  setupMode?: boolean
}>(), {
  setupMode: false,
})

const emit = defineEmits<{ changed: [] }>()
const providers = ref<ProviderDescriptor[]>([])
const loading = ref(false)
const saving = ref(false)
const editing = ref(false)
const editingExisting = ref(false)
const restartRequired = ref(false)
const setupAutoOpened = ref(false)

const form = reactive<ProviderFormState>(emptyForm())
const configurableProviders = computed(() =>
  providers.value.filter((provider) => provider.config.adapter === 'openai_compatible'),
)
const bindTargets = computed(() => [
  { id: 'main', displayName: '任务管家和检查组', description: '负责读懂需求、安排分工、检查结果和整理回复' },
  ...props.agents
    .filter((agent) => agent.assignable)
    .map((agent) => ({
      id: agent.id,
      displayName: agent.display_name,
      description: '可以单独选择自己的模型',
    })),
])
const availableModels = computed(() =>
  [...new Set(form.allowedModels.map((item) => item.trim()).filter(Boolean))],
)

function emptyForm(): ProviderFormState {
  return {
    name: '',
    protocolVariant: 'openai',
    apiFamily: 'chat_completions',
    enabled: false,
    baseUrl: '',
    baseUrlEnv: '',
    credentialSource: 'stored',
    apiKeyEnv: '',
    apiKey: '',
    credentialConfigured: false,
    clearApiKey: false,
    allowedModels: [],
    structuredOutputMode: '',
    chatTokenParameter: '',
    storeResponses: false,
    timeoutSeconds: 60,
    maxConcurrency: 8,
    allowInsecureHttp: false,
    allowPrivateNetwork: false,
    options: {},
    defaultModel: '',
    modelBindings: {},
    agentBindings: {},
  }
}

function replaceForm(value: ProviderFormState): void {
  Object.assign(form, value)
}

function startCreate(): void {
  replaceForm(emptyForm())
  editingExisting.value = false
  editing.value = true
}

function startEdit(provider: ProviderDescriptor): void {
  const config = provider.config
  const bindingValues = props.logicalModels.map((logical) => provider.model_bindings[logical])
  const sharedDefault = bindingValues.length > 0
    && bindingValues.every((model) => model && model === bindingValues[0])
    ? (bindingValues[0] ?? '')
    : ''
  const usePageCredential = props.setupMode
    && provider.source === 'yaml'
    && provider.endpoint_available === false
    && provider.credential_available === false
  replaceForm({
    name: provider.name,
    protocolVariant: config.protocol_variant,
    apiFamily: config.api_family,
    enabled: config.enabled,
    baseUrl: config.base_url ?? '',
    baseUrlEnv: config.base_url_env ?? '',
    credentialSource: usePageCredential ? 'stored' : config.credential_source ?? 'environment',
    apiKeyEnv: config.api_key_env ?? '',
    apiKey: '',
    credentialConfigured:
      config.credential_source === 'stored' && provider.credential_available === true,
    clearApiKey: false,
    allowedModels: [...config.allowed_models],
    structuredOutputMode: config.structured_output_mode ?? '',
    chatTokenParameter: config.chat_token_parameter ?? '',
    storeResponses: config.store_responses,
    timeoutSeconds: config.timeout_seconds,
    maxConcurrency: config.max_concurrency,
    allowInsecureHttp: config.allow_insecure_http,
    allowPrivateNetwork: config.allow_private_network,
    options: { ...config.options },
    defaultModel: sharedDefault,
    modelBindings: { ...provider.model_bindings },
    agentBindings: { ...provider.agent_bindings },
  })
  editingExisting.value = true
  editing.value = true
}

function cancelEdit(): void {
  editing.value = false
  editingExisting.value = false
}

function compactBindings(bindings: Record<string, string>): Record<string, string> {
  return Object.fromEntries(
    Object.entries(bindings)
      .map(([key, value]) => [key, value.trim()])
      .filter(([, value]) => Boolean(value)),
  )
}

function applyDefaultModel(model: string): void {
  form.defaultModel = model
  if (!model) return
  for (const logical of props.logicalModels) form.modelBindings[logical] = model
}

function syncAllowedModels(values: string[]): void {
  let normalized = [...new Set(values.map((item) => item.trim()).filter(Boolean))]
  if (
    props.setupMode
    && normalized.length > 1
    && normalized.includes('configured-model')
  ) {
    normalized = normalized.filter((model) => model !== 'configured-model')
  }
  form.allowedModels = normalized
  const allowed = new Set(normalized)
  if (!allowed.has(form.defaultModel)) {
    form.defaultModel = normalized.length === 1 ? (normalized[0] ?? '') : ''
  }
  for (const [logical, model] of Object.entries(form.modelBindings)) {
    if (!allowed.has(model)) delete form.modelBindings[logical]
  }
  for (const [agent, model] of Object.entries(form.agentBindings)) {
    if (!allowed.has(model)) delete form.agentBindings[agent]
  }
  if (form.defaultModel) applyDefaultModel(form.defaultModel)
}

function validateForm(): string | null {
  if (!/^[a-z][a-z0-9_-]{1,63}$/.test(form.name)) {
    return '配置名称需以小写字母开头，只能包含小写字母、数字、- 和 _'
  }
  if (availableModels.value.length === 0) return '至少需要一个可用模型 ID'
  if (props.setupMode && availableModels.value.includes('configured-model')) {
    return '请把示例模型名称替换为服务实际支持的模型名称'
  }
  if (
    form.enabled
    && !form.baseUrl.trim()
    && (form.credentialSource !== 'environment' || !form.baseUrlEnv.trim())
  ) {
    return '启用模型服务前，需要配置 Base URL 或对应的环境变量名'
  }
  if (
    form.enabled
    && form.credentialSource === 'environment'
    && !form.apiKeyEnv.trim()
  ) {
    return '使用环境变量时，需要填写 API Key 环境变量名'
  }
  if (
    form.enabled
    && form.credentialSource === 'stored'
    && !form.apiKey.trim()
    && (!form.credentialConfigured || form.clearApiKey)
  ) {
    return '使用内置密钥库时，需要填写 API Key'
  }
  const allowed = new Set(availableModels.value)
  const unavailable = [
    ...Object.values(compactBindings(form.modelBindings)),
    ...Object.values(compactBindings(form.agentBindings)),
  ].filter((model) => !allowed.has(model))
  if (unavailable.length > 0) return `成员分配中使用了尚未添加的模型：${unavailable.join('、')}`
  if (
    props.setupMode
    && form.enabled
    && props.logicalModels.some((logical) => !form.modelBindings[logical]?.trim())
  ) {
    return '首次配置需要选择默认工作模型，系统会自动分配给各个工作阶段'
  }
  return null
}

function profilePayload(): ProviderProfileSpec {
  return {
    config: {
      adapter: 'openai_compatible',
      protocol_variant: form.protocolVariant,
      api_family: form.apiFamily,
      structured_output_mode: form.structuredOutputMode || null,
      chat_token_parameter: form.chatTokenParameter || null,
      store_responses: form.storeResponses,
      enabled: form.enabled,
      base_url: form.baseUrl.trim() || null,
      base_url_env:
        form.credentialSource === 'environment' ? form.baseUrlEnv.trim() || null : null,
      credential_source: form.credentialSource,
      api_key_env:
        form.credentialSource === 'environment' ? form.apiKeyEnv.trim() || null : null,
      allowed_models: availableModels.value,
      capabilities: ['chat', 'structured_output', 'tool_calling'],
      timeout_seconds: form.timeoutSeconds,
      max_concurrency: form.maxConcurrency,
      allow_insecure_http: form.allowInsecureHttp,
      allow_private_network: form.allowPrivateNetwork,
      options: { ...form.options },
    },
    model_bindings: compactBindings(form.modelBindings),
    agent_bindings: compactBindings(form.agentBindings),
    api_key:
      form.credentialSource === 'stored' ? form.apiKey.trim() || undefined : undefined,
    clear_api_key: form.clearApiKey || form.credentialSource === 'environment',
  }
}

async function loadProviders(): Promise<void> {
  loading.value = true
  try {
    const response = await harnessApi.providers()
    providers.value = response.items
    restartRequired.value = response.restart_required
    if (props.setupMode && !setupAutoOpened.value && !editing.value) {
      const initialProvider = response.items.find(
        (provider) => provider.name === 'compatible'
          && provider.config.adapter === 'openai_compatible',
      ) ?? response.items.find(
        (provider) => provider.config.adapter === 'openai_compatible',
      )
      if (initialProvider) {
        setupAutoOpened.value = true
        startEdit(initialProvider)
      }
    }
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '无法读取模型服务配置')
  } finally {
    loading.value = false
  }
}

async function saveProvider(): Promise<void> {
  const issue = validateForm()
  if (issue) {
    ElMessage.warning(issue)
    return
  }
  saving.value = true
  try {
    const result = await harnessApi.saveProvider(form.name, profilePayload())
    restartRequired.value = result.restart_required
    await loadProviders()
    editing.value = false
    emit('changed')
    ElMessage.success('设置已保存，请重启 Work Harness 使新配置生效')
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '模型服务保存失败')
  } finally {
    saving.value = false
  }
}

async function deleteProvider(provider: ProviderDescriptor): Promise<void> {
  try {
    await harnessApi.deleteProvider(provider.name)
    await loadProviders()
    emit('changed')
    ElMessage.success(
      provider.name === 'compatible'
        ? '默认模型服务已恢复为内置初始设置，请重启后生效'
        : `模型服务 ${providerDisplayName(provider)} 已删除，请重启后生效`,
    )
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '模型服务删除失败')
  }
}

function statusLabel(provider: ProviderDescriptor): string {
  if (!provider.config.enabled) return '未启用'
  if (!provider.loaded) return '待重启'
  if (provider.endpoint_available === false) return '缺少接口地址'
  if (provider.credential_available === false) return '缺少密钥'
  if (provider.config.allowed_models.includes('configured-model')) return '需要填写模型'
  return '已就绪'
}

function statusType(provider: ProviderDescriptor): 'success' | 'warning' | 'info' | 'danger' {
  if (!provider.config.enabled) return 'info'
  if (!provider.loaded) return 'warning'
  if (provider.endpoint_available === false || provider.credential_available === false) {
    return 'danger'
  }
  if (provider.config.allowed_models.includes('configured-model')) return 'danger'
  return 'success'
}

function providerDisplayName(provider: ProviderDescriptor): string {
  return provider.name === 'compatible' ? '默认模型服务' : provider.name
}

function providerSourceLabel(provider: ProviderDescriptor): string {
  if (provider.source === 'persistent') return '页面保存'
  if (provider.source === 'removed') return '已删除，等待重启'
  return '内置初始配置'
}

function protocolLabel(provider: ProviderDescriptor): string {
  return provider.config.protocol_variant === 'deepseek'
    ? 'DeepSeek 兼容格式'
    : 'OpenAI 兼容格式'
}

function apiFamilyLabel(provider: ProviderDescriptor): string {
  return provider.config.api_family === 'responses'
    ? 'Responses 请求格式'
    : '对话补全请求格式'
}

function endpointLabel(provider: ProviderDescriptor): string {
  if (provider.config.base_url) return provider.config.base_url
  if (provider.config.base_url_env) return '由部署环境提供'
  return '未配置'
}

function credentialLabel(provider: ProviderDescriptor): string {
  const configured = provider.credential_available ? '已检测' : '未检测'
  return provider.config.credential_source === 'stored'
    ? `页面加密保存 · ${configured}`
    : `由部署环境提供 · ${configured}`
}

function agentBindingLabel(agentId: string): string {
  if (agentId === 'main') return '任务管家和检查组'
  return props.agents.find((agent) => agent.id === agentId)?.display_name ?? '指定群成员'
}

function deleteActionLabel(provider: ProviderDescriptor): string {
  return provider.name === 'compatible' ? '恢复初始设置' : '删除服务'
}

function deleteConfirmTitle(provider: ProviderDescriptor): string {
  return provider.name === 'compatible'
    ? '恢复默认模型服务的内置初始设置？'
    : `删除模型服务 ${providerDisplayName(provider)}？`
}

onMounted(() => {
  setupAutoOpened.value = false
  void loadProviders()
})
</script>

<template>
  <section class="provider-settings-panel">
    <header class="settings-content-heading">
      <div>
        <p class="eyebrow">模型与路由</p>
        <h2>{{ setupMode ? '完成首次模型配置' : '模型服务与成员分配' }}</h2>
        <p>适用于 OpenAI 兼容服务和 DeepSeek 兼容服务。</p>
      </div>
    </header>

    <div class="provider-settings">
      <el-alert
        v-if="setupMode"
        title="连接真实模型后才能开始任务"
        description="填写接口地址、密钥和模型名称，保存后重启服务。未完成配置时，系统不会再用模拟结果代替子成员工作。"
        type="warning"
        show-icon
        :closable="false"
      />
      <el-alert
        v-if="restartRequired"
        title="新设置正在等待重启"
        description="重启 Work Harness 后会装载新设置；已经开始的任务不会中途切换模型。"
        type="warning"
        show-icon
        :closable="false"
      />
      <el-alert
        v-if="!setupMode"
        title="密钥可以用两种方式提供"
        description="既可以在本页面加密保存，也可以只填写变量名称，由 Docker 或部署平台注入。页面和 API 都不会回显密钥明文。"
        type="info"
        show-icon
        :closable="false"
      />

      <template v-if="!editing">
        <div class="provider-toolbar">
          <div>
            <strong>模型服务</strong>
            <small>可以修改内置初始配置，也可以添加多个自定义模型服务。</small>
          </div>
          <span>
            <el-button :icon="Refresh" circle aria-label="刷新模型服务" @click="loadProviders" />
            <el-button type="primary" :icon="Plus" @click="startCreate">添加服务</el-button>
          </span>
        </div>

        <el-skeleton v-if="loading" :rows="5" animated />
        <el-empty v-else-if="configurableProviders.length === 0" description="还没有模型服务" />
        <div v-else class="provider-list">
          <article
            v-for="provider in configurableProviders"
            :key="provider.name"
            class="provider-card"
          >
            <div class="provider-card-heading">
              <div>
                <span class="provider-name-row">
                  <strong>{{ providerDisplayName(provider) }}</strong>
                  <el-tag size="small" effect="plain">{{ providerSourceLabel(provider) }}</el-tag>
                  <el-tag size="small" effect="plain" :type="statusType(provider)">
                    {{ statusLabel(provider) }}
                  </el-tag>
                </span>
                <small>{{ protocolLabel(provider) }} · {{ apiFamilyLabel(provider) }}</small>
              </div>
              <span class="provider-card-actions">
                <el-button text :icon="EditPen" @click="startEdit(provider)">编辑设置</el-button>
                <el-popconfirm
                  v-if="provider.source === 'persistent'"
                  :title="deleteConfirmTitle(provider)"
                  :confirm-button-text="provider.name === 'compatible' ? '恢复' : '删除'"
                  cancel-button-text="取消"
                  width="280"
                  @confirm="deleteProvider(provider)"
                >
                  <template #reference>
                    <el-button text type="danger" :icon="Delete">
                      {{ deleteActionLabel(provider) }}
                    </el-button>
                  </template>
                </el-popconfirm>
              </span>
            </div>

            <div class="provider-facts">
              <span><b>接口地址</b>{{ endpointLabel(provider) }}</span>
              <span><b>密钥方式</b>{{ credentialLabel(provider) }}</span>
              <span><b>可用模型</b>{{ provider.config.allowed_models.join('、') || '未填写' }}</span>
              <span><b>同时请求数</b>{{ provider.config.max_concurrency }}</span>
            </div>

            <div
              v-if="Object.keys(provider.model_bindings).length || Object.keys(provider.agent_bindings).length"
              class="provider-routes"
            >
              <el-tag
                v-for="(model, logical) in provider.model_bindings"
                :key="`logical:${logical}`"
                size="small"
                effect="plain"
              >
                {{ logicalModelLabel(logical) }} → {{ model }}
              </el-tag>
              <el-tag
                v-for="(model, agent) in provider.agent_bindings"
                :key="`agent:${agent}`"
                size="small"
                effect="plain"
                type="warning"
              >
                {{ agentBindingLabel(agent) }} → {{ model }}
              </el-tag>
            </div>
          </article>
        </div>
      </template>

      <el-form v-else label-position="top" class="provider-form" @submit.prevent>
        <div class="provider-form-heading">
          <div>
            <strong>
              {{ editingExisting ? `编辑${form.name === 'compatible' ? '默认模型服务' : form.name}` : '添加模型服务' }}
            </strong>
            <small>这里的 OpenAI 表示兼容格式，不限定官方服务，也不限制模型名称。</small>
          </div>
          <el-button text @click="cancelEdit">查看服务列表</el-button>
        </div>

        <section class="provider-form-section">
          <h3>连接信息</h3>
          <div class="provider-form-grid">
            <el-form-item v-if="!editingExisting" label="配置名称（用于区分多个服务）">
              <el-input v-model="form.name" placeholder="例如 team-gateway" />
            </el-form-item>
            <el-form-item label="是否用于任务">
              <el-switch
                v-model="form.enabled"
                inline-prompt
                active-text="启用"
                inactive-text="暂存"
              />
            </el-form-item>
            <el-form-item label="接口兼容类型">
              <el-select v-model="form.protocolVariant">
                <el-option label="OpenAI 兼容格式" value="openai" />
                <el-option label="DeepSeek 兼容格式" value="deepseek" />
              </el-select>
            </el-form-item>
            <el-form-item label="请求格式">
              <el-select v-model="form.apiFamily">
                <el-option label="对话补全（兼容范围更广）" value="chat_completions" />
                <el-option label="Responses（服务明确支持时）" value="responses" />
              </el-select>
            </el-form-item>
            <el-form-item label="接口地址（Base URL）">
              <el-input v-model="form.baseUrl" placeholder="例如 https://gateway.example/v1" />
            </el-form-item>
            <el-form-item label="密钥提供方式">
              <el-select v-model="form.credentialSource">
                <el-option label="在本页面加密保存" value="stored" />
                <el-option label="由部署环境提供" value="environment" />
              </el-select>
            </el-form-item>
            <el-form-item
              v-if="form.credentialSource === 'stored'"
              :label="form.credentialConfigured ? '模型服务密钥（留空保持不变）' : '模型服务密钥'"
            >
              <el-input
                v-model="form.apiKey"
                type="password"
                show-password
                autocomplete="new-password"
                placeholder="输入 API Key"
                @input="form.clearApiKey = false"
              />
            </el-form-item>
            <template v-else>
              <el-form-item label="接口地址变量名（可选）">
                <el-input v-model="form.baseUrlEnv" placeholder="例如 TEAM_MODEL_BASE_URL" />
              </el-form-item>
              <el-form-item label="密钥变量名">
                <el-input v-model="form.apiKeyEnv" placeholder="例如 TEAM_MODEL_API_KEY" />
              </el-form-item>
            </template>
            <el-form-item
              v-if="form.credentialSource === 'stored' && form.credentialConfigured"
              label="已保存的密钥"
            >
              <el-checkbox v-model="form.clearApiKey">删除原来的密钥</el-checkbox>
            </el-form-item>
          </div>
        </section>

        <section class="provider-form-section">
          <h3>模型选择</h3>
          <p>先添加服务实际支持的模型名称，再选一个默认工作模型。首次配置只需要完成这两项。</p>
          <el-form-item label="这个服务可以使用哪些模型？">
            <el-select
              v-model="form.allowedModels"
              multiple
              filterable
              allow-create
              default-first-option
              placeholder="输入模型名称后按回车，可添加多个"
              @change="syncAllowedModels"
            >
              <el-option
                v-for="model in availableModels"
                :key="model"
                :label="model"
                :value="model"
              />
            </el-select>
          </el-form-item>
          <el-form-item label="默认工作模型（推荐）">
            <el-select
              v-model="form.defaultModel"
              clearable
              placeholder="选择后会自动分配给所有工作阶段"
              @change="applyDefaultModel"
            >
              <el-option
                v-for="model in availableModels"
                :key="model"
                :label="model"
                :value="model"
              />
            </el-select>
          </el-form-item>

          <details class="provider-advanced">
            <summary>按工作阶段分别选择模型（可选）</summary>
            <p>只有在不同阶段需要不同模型时才修改；留在默认值即可使用上面的工作模型。</p>
            <div class="binding-list">
              <label v-for="logical in logicalModels" :key="logical">
                <span><strong>{{ logicalModelLabel(logical) }}</strong></span>
                <el-select
                  v-model="form.modelBindings[logical]"
                  clearable
                  placeholder="使用默认工作模型"
                >
                  <el-option
                    v-for="model in availableModels"
                    :key="model"
                    :label="model"
                    :value="model"
                  />
                </el-select>
              </label>
            </div>
          </details>
        </section>

        <section class="provider-form-section">
          <details class="provider-advanced">
            <summary>给任务管家或群成员单独选模型（可选）</summary>
            <p>单独设置会优先于阶段默认值；不选择时会自动继承。</p>
            <div class="binding-list">
              <label v-for="target in bindTargets" :key="target.id">
                <span>
                  <strong>{{ target.displayName }}</strong>
                  <small>{{ target.description }}</small>
                </span>
                <el-select
                  v-model="form.agentBindings[target.id]"
                  clearable
                  placeholder="自动继承"
                >
                  <el-option
                    v-for="model in availableModels"
                    :key="model"
                    :label="model"
                    :value="model"
                  />
                </el-select>
              </label>
            </div>
          </details>
        </section>

        <section class="provider-form-section">
          <details class="provider-advanced">
            <summary>高级兼容与网络选项（通常不用修改）</summary>
            <div class="provider-form-grid provider-advanced-grid">
              <el-form-item label="结构化内容格式">
                <el-select v-model="form.structuredOutputMode">
                  <el-option label="自动选择" value="" />
                  <el-option label="严格结构化格式" value="json_schema" />
                  <el-option label="通用 JSON 对象" value="json_object" />
                </el-select>
              </el-form-item>
              <el-form-item label="输出长度参数">
                <el-select v-model="form.chatTokenParameter">
                  <el-option label="自动选择" value="" />
                  <el-option label="新版兼容参数" value="max_completion_tokens" />
                  <el-option label="传统兼容参数" value="max_tokens" />
                </el-select>
              </el-form-item>
              <el-form-item label="请求超时（秒）">
                <el-input-number v-model="form.timeoutSeconds" :min="1" :max="600" />
              </el-form-item>
              <el-form-item label="同时请求数">
                <el-input-number v-model="form.maxConcurrency" :min="1" :max="256" />
              </el-form-item>
            </div>
            <div class="provider-switches">
              <el-checkbox v-model="form.storeResponses">允许服务端保留 Responses 响应</el-checkbox>
              <el-checkbox v-model="form.allowPrivateNetwork">允许连接局域网或本机服务</el-checkbox>
              <el-checkbox v-model="form.allowInsecureHttp">允许使用不加密的 HTTP 地址</el-checkbox>
            </div>
          </details>
        </section>

        <div class="provider-form-actions">
          <el-button @click="cancelEdit">取消</el-button>
          <el-button type="primary" :icon="CircleCheck" :loading="saving" @click="saveProvider">
            保存设置
          </el-button>
        </div>
      </el-form>
    </div>
  </section>
</template>
