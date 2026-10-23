<script setup lang="ts">
import ConfirmAction from './ConfirmAction.vue'
import ModelCatalog from './ModelCatalog.vue'
import { computed, onMounted, onBeforeUnmount, reactive, ref, watch } from 'vue'
import { useNotice } from '../composables/useNotice'

import { harnessApi } from '../services/api'
import { logicalModelLabel } from '../utils/labels'
import type {
  AgentDescriptor,
  UpstreamModel,
  ProviderApiFamily,
  ProviderCredentialSource,
  ProviderDescriptor,
  ProviderProfileSpec,
  ProviderProtocolVariant,
} from '../types'

import ModelReasoningSettings from './ModelReasoningSettings.vue'
import type { ModelSettings } from '../types'

type StructuredOutputMode = '' | 'json_schema' | 'json_object'
type ChatTokenParameter = '' | 'max_completion_tokens' | 'max_tokens'

interface ProviderFormState {
  name: string
  protocolVariant: ProviderProtocolVariant
  authScheme: 'auto' | 'bearer' | 'x_api_key'
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
  options: Record<string, unknown>
  defaultModel: string
  modelSettings: Record<string, ModelSettings>
  modelBindings: Record<string, string>
  agentBindings: Record<string, string>
}

const notice = useNotice()

const props = withDefaults(
  defineProps<{
    logicalModels: string[]
    agents: AgentDescriptor[]
    setupMode?: boolean
  }>(),
  {
    setupMode: false,
  },
)

const emit = defineEmits<{ changed: [] }>()
const providers = ref<ProviderDescriptor[]>([])
const loading = ref(false)
const loadFailed = ref(false)
const saving = ref(false)
const editing = ref(false)
const editingExisting = ref(false)
const restartRequired = ref(false)
const setupAutoOpened = ref(false)
const upstreamModels = ref<UpstreamModel[]>([])
const discovering = ref(false)
const discoveryError = ref('')
const discoveryFetched = ref(false)
const discoveryTruncated = ref(false)
let discoveryController: AbortController | undefined

const form = reactive<ProviderFormState>(emptyForm())
const configurableProviders = computed(() =>
  providers.value.filter((provider) =>
    ['openai_compatible', 'anthropic'].includes(provider.config.adapter),
  ),
)
const bindTargets = computed(() => [
  {
    id: 'main',
    displayName: '协调与核验 Agent',
    description: '负责读懂需求、安排分工、检查结果和整理回复',
  },
  ...props.agents
    .filter((agent) => agent.assignable)
    .map((agent) => ({
      id: agent.id,
      displayName: agent.display_name,
      description: '可以单独选择自己的模型',
    })),
])
const availableModels = computed(() => [
  ...new Set(form.allowedModels.map((item) => item.trim()).filter(Boolean)),
])

function emptyForm(): ProviderFormState {
  return {
    name: '',
    protocolVariant: 'openai',
    apiFamily: 'chat_completions',
    authScheme: 'auto',
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
    options: {},
    defaultModel: '',
    modelSettings: {},
    modelBindings: {},
    agentBindings: {},
  }
}

let replacingForm = false
function replaceForm(value: ProviderFormState): void {
  resetDiscovery()
  replacingForm = true
  Object.assign(form, value)
  replacingForm = false
}

function startCreate(): void {
  replaceForm(emptyForm())
  editingExisting.value = false
  editing.value = true
}

function startEdit(provider: ProviderDescriptor): void {
  const config = provider.config
  const bindingValues = props.logicalModels.map((logical) => provider.model_bindings[logical])
  const sharedDefault =
    bindingValues.length > 0 && bindingValues.every((model) => model && model === bindingValues[0])
      ? (bindingValues[0] ?? '')
      : ''
  const usePageCredential =
    props.setupMode &&
    provider.source === 'yaml' &&
    provider.endpoint_available === false &&
    provider.credential_available === false
  replaceForm({
    name: provider.name,
    protocolVariant: config.protocol_variant,
    apiFamily: config.api_family,
    authScheme: config.auth_scheme ?? 'auto',
    enabled: config.enabled,
    baseUrl: config.base_url ?? '',
    baseUrlEnv: config.base_url_env ?? '',
    credentialSource: usePageCredential ? 'stored' : (config.credential_source ?? 'environment'),
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
    options: { ...config.options },
    defaultModel: sharedDefault,
    modelSettings: JSON.parse(JSON.stringify(config.model_settings ?? {})),
    modelBindings: { ...provider.model_bindings },
    agentBindings: { ...provider.agent_bindings },
  })
  editingExisting.value = true
  editing.value = true
}

function cancelEdit(): void {
  resetDiscovery()
  form.apiKey = ''
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
  if (props.setupMode && normalized.length > 1 && normalized.includes('configured-model')) {
    normalized = normalized.filter((model) => model !== 'configured-model')
  }
  form.allowedModels = normalized
  const allowed = new Set(normalized)
  form.modelSettings = Object.fromEntries(
    Object.entries(form.modelSettings).filter(([model]) => allowed.has(model)),
  )
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
    form.enabled &&
    !form.baseUrl.trim() &&
    (form.credentialSource !== 'environment' || !form.baseUrlEnv.trim())
  ) {
    return '启用模型服务前，需要配置 Base URL 或对应的环境变量名'
  }
  if (form.enabled && form.credentialSource === 'environment' && !form.apiKeyEnv.trim()) {
    return '使用环境变量时，需要填写 API Key 环境变量名'
  }
  if (
    form.enabled &&
    form.credentialSource === 'stored' &&
    !form.apiKey.trim() &&
    (!form.credentialConfigured || form.clearApiKey)
  ) {
    return '使用内置密钥库时，需要填写 API Key'
  }
  const allowed = new Set(availableModels.value)
  const unavailable = [
    ...Object.values(compactBindings(form.modelBindings)),
    ...Object.values(compactBindings(form.agentBindings)),
  ].filter((model) => !allowed.has(model))
  if (unavailable.length > 0) return `Agent 分配中使用了尚未添加的模型：${unavailable.join('、')}`
  if (
    props.setupMode &&
    form.enabled &&
    props.logicalModels.some((logical) => !form.modelBindings[logical]?.trim())
  ) {
    return '首次配置需要选择默认工作模型，系统会自动分配给各个工作阶段'
  }
  return null
}

function profilePayload(): ProviderProfileSpec {
  return {
    config: {
      adapter: form.apiFamily === 'messages' ? 'anthropic' : 'openai_compatible',
      auth_scheme: form.authScheme,
      protocol_variant: form.apiFamily === 'chat_completions' ? form.protocolVariant : 'openai',
      api_family: form.apiFamily,
      structured_output_mode: form.structuredOutputMode || null,
      chat_token_parameter:
        form.apiFamily === 'chat_completions' ? form.chatTokenParameter || null : null,
      store_responses: form.apiFamily === 'responses' && form.storeResponses,
      enabled: form.enabled,
      base_url: form.baseUrl.trim() || null,
      base_url_env: form.credentialSource === 'environment' ? form.baseUrlEnv.trim() || null : null,
      credential_source: form.credentialSource,
      api_key_env: form.credentialSource === 'environment' ? form.apiKeyEnv.trim() || null : null,
      allowed_models: availableModels.value,
      model_settings: form.modelSettings,
      capabilities: ['chat', 'structured_output', 'tool_calling'],
      timeout_seconds: form.timeoutSeconds,
      max_concurrency: form.maxConcurrency,
      allow_insecure_http: null,
      allow_private_network: null,
      options: { ...form.options },
    },
    model_bindings: compactBindings(form.modelBindings),
    agent_bindings: compactBindings(form.agentBindings),
    api_key: form.credentialSource === 'stored' ? form.apiKey.trim() || undefined : undefined,
    clear_api_key: form.clearApiKey || form.credentialSource === 'environment',
  }
}

async function loadProviders(): Promise<void> {
  loading.value = true
  loadFailed.value = false
  try {
    const response = await harnessApi.providers()
    providers.value = response.items
    restartRequired.value = response.restart_required
    if (props.setupMode && !setupAutoOpened.value && !editing.value) {
      const initialProvider =
        response.items.find(
          (provider) =>
            provider.name === 'compatible' &&
            ['openai_compatible', 'anthropic'].includes(provider.config.adapter),
        ) ??
        response.items.find((provider) =>
          ['openai_compatible', 'anthropic'].includes(provider.config.adapter),
        )
      if (initialProvider) {
        setupAutoOpened.value = true
        startEdit(initialProvider)
      }
    }
  } catch (error) {
    loadFailed.value = true
    notice.error(error instanceof Error ? error.message : '无法读取模型服务配置')
  } finally {
    loading.value = false
  }
}

async function saveProvider(): Promise<void> {
  const issue = validateForm()
  if (issue) {
    notice.warning(issue)
    return
  }
  saving.value = true
  try {
    const result = await harnessApi.saveProvider(form.name, profilePayload())
    restartRequired.value = result.restart_required
    await loadProviders()
    cancelEdit()
    emit('changed')
    notice.success('设置已保存，请重启 Work Harness 使新配置生效')
  } catch (error) {
    notice.error(error instanceof Error ? error.message : '模型服务保存失败')
  } finally {
    saving.value = false
  }
}

async function deleteProvider(provider: ProviderDescriptor): Promise<void> {
  try {
    await harnessApi.deleteProvider(provider.name)
    await loadProviders()
    emit('changed')
    notice.success(
      provider.name === 'compatible'
        ? '默认模型服务已恢复为内置初始设置，请重启后生效'
        : `模型服务 ${providerDisplayName(provider)} 已删除，请重启后生效`,
    )
  } catch (error) {
    notice.error(error instanceof Error ? error.message : '模型服务删除失败')
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

function statusType(provider: ProviderDescriptor): 'success' | 'warning' | 'info' | 'error' {
  if (!provider.config.enabled) return 'info'
  if (!provider.loaded) return 'warning'
  if (provider.endpoint_available === false || provider.credential_available === false) {
    return 'error'
  }
  if (provider.config.allowed_models.includes('configured-model')) return 'error'
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
  return provider.config.adapter === 'anthropic'
    ? 'Anthropic 兼容'
    : provider.config.protocol_variant === 'deepseek'
      ? 'DeepSeek 兼容'
      : 'OpenAI 兼容'
}

function apiFamilyLabel(provider: ProviderDescriptor): string {
  return (
    protocolOptions.find((item) => item.value === provider.config.api_family)?.label ?? '自定义协议'
  )
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
  if (agentId === 'main') return '协调与核验 Agent'
  return props.agents.find((agent) => agent.id === agentId)?.display_name ?? '指定 Agent'
}

function deleteActionLabel(provider: ProviderDescriptor): string {
  return provider.name === 'compatible' ? '恢复初始设置' : '删除服务'
}

function deleteConfirmTitle(provider: ProviderDescriptor): string {
  return provider.name === 'compatible'
    ? '恢复默认模型服务的内置初始设置？'
    : `删除模型服务 ${providerDisplayName(provider)}？`
}

const protocolOptions = [
  {
    value: 'responses',
    label: 'OpenAI Responses',
    path: 'responses',
    description: '使用 Responses 接口的 OpenAI 服务及兼容网关。',
  },
  {
    value: 'chat_completions',
    label: 'OpenAI Chat Completions',
    path: 'chat/completions',
    description: '适用于常见的 OpenAI 兼容服务，也可切换 DeepSeek 参数。',
  },
  {
    value: 'messages',
    label: 'Claude Messages',
    path: 'messages',
    description: '使用 Anthropic 原生消息与工具格式，支持 Claude 兼容渠道。',
  },
] as const
const activeProtocol = computed(() =>
  protocolOptions.find((item) => item.value === form.apiFamily)!,
)
function resetDiscovery(): void {
  discoveryController?.abort()
  discovering.value = false
  upstreamModels.value = []
  discoveryError.value = ''
  discoveryFetched.value = false
  discoveryTruncated.value = false
}

watch(
  () => [
    form.name,
    form.baseUrl,
    form.baseUrlEnv,
    form.apiFamily,
    form.authScheme,
    form.credentialSource,
    form.apiKey,
    form.apiKeyEnv,
    form.clearApiKey,
  ],
  resetDiscovery,
)
const verifyingModel = ref(false)
const verificationConnection = computed(() => {
  const profile = profilePayload()
  return {
    config: { ...profile.config, model_settings: {} },
    provider_name: editingExisting.value ? form.name : undefined,
    api_key: profile.api_key,
    clear_api_key: profile.clear_api_key,
  }
})
watch(
  () => [
    form.name,
    form.baseUrl,
    form.baseUrlEnv,
    form.apiFamily,
    form.authScheme,
    form.credentialSource,
    form.apiKey,
    form.apiKeyEnv,
    form.clearApiKey,
    form.protocolVariant,
    form.structuredOutputMode,
    form.chatTokenParameter,
    form.storeResponses,
  ],
  () => {
    if (!replacingForm) form.modelSettings = {}
  },
  { flush: 'sync' },
)
watch(
  () => form.baseUrl,
  (value) => {
    if (form.credentialSource === 'environment' && form.baseUrlEnv.trim()) return
    // A complete endpoint identifies its wire format. Root URLs keep the selected protocol.
    const path = value.trim().split(/[?#]/, 1)[0]?.replace(/\/+$/, '') ?? ''
    const protocol = protocolOptions.find((item) => path.endsWith(`/${item.path}`))
    if (protocol) form.apiFamily = protocol.value
  },
)
onBeforeUnmount(() => discoveryController?.abort())

async function fetchModels(): Promise<void> {
  resetDiscovery()
  const controller = new AbortController()
  discoveryController = controller
  discovering.value = true
  const profile = profilePayload()
  try {
    const result = await harnessApi.discoverModels(
      {
        config: { ...profile.config, enabled: false },
        provider_name: editingExisting.value ? form.name : undefined,
        api_key: profile.api_key,
        clear_api_key: profile.clear_api_key,
      },
      controller.signal,
    )
    if (controller.signal.aborted) return
    upstreamModels.value = result.items
    discoveryTruncated.value = result.truncated
    discoveryFetched.value = true
  } catch (error) {
    if (!controller.signal.aborted)
      discoveryError.value = error instanceof Error ? error.message : '模型获取失败'
  } finally {
    if (!controller.signal.aborted) discovering.value = false
  }
}

function toggleUpstreamModel(model: string): void {
  syncAllowedModels(
    form.allowedModels.includes(model)
      ? form.allowedModels.filter((item) => item !== model)
      : [...form.allowedModels, model],
  )
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
        <h2>{{ setupMode ? '完成首次模型配置' : '模型连接' }}</h2>
        <p>连接官方 API 或 new-api、sub2api 等网关，获取模型并分配给任务与 Agent。</p>
      </div>
    </header>

    <div class="provider-settings">
      <UAlert
        v-if="setupMode"
        title="连接真实模型后才能开始任务"
        description="选择协议，填写上游地址与密钥，再获取并选择模型。保存后重启服务即可开始任务。"
        color="warning"
        variant="subtle"
      />
      <UAlert
        v-if="restartRequired"
        title="新设置正在等待重启"
        description="重启 Work Harness 后会装载新设置；已经开始的任务不会中途切换模型。"
        color="warning"
        variant="subtle"
      />
      <template v-if="!editing">
        <div class="provider-toolbar">
          <div>
            <strong>模型服务</strong>
            <small>可以修改内置初始配置，也可以添加多个自定义模型服务。</small>
          </div>
          <span>
            <UButton
              icon="i-lucide-refresh-cw"
              v-if="loadFailed"
              aria-label="重新读取模型服务"
              @click="loadProviders"
              color="neutral"
              variant="outline"
            />
            <UButton color="neutral" icon="i-lucide-plus" @click="startCreate">添加服务</UButton>
          </span>
        </div>

        <USkeleton v-if="loading" />
        <UEmpty v-else-if="configurableProviders.length === 0" title="还没有模型服务" />
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
                  <UBadge size="sm" variant="subtle">{{ providerSourceLabel(provider) }}</UBadge>
                  <UBadge size="sm" :color="statusType(provider)" variant="subtle">
                    {{ statusLabel(provider) }}
                  </UBadge>
                </span>
                <small>{{ protocolLabel(provider) }} · {{ apiFamilyLabel(provider) }}</small>
              </div>
              <span class="provider-card-actions">
                <UButton
                  variant="ghost"
                  icon="i-lucide-pencil"
                  @click="startEdit(provider)"
                  color="neutral"
                  >编辑设置</UButton
                >
                <ConfirmAction
                  v-if="provider.source === 'persistent'"
                  :title="deleteConfirmTitle(provider)"
                  :confirm-button-text="provider.name === 'compatible' ? '恢复' : '删除'"
                  cancel-button-text="取消"
                  width="280"
                  @confirm="deleteProvider(provider)"
                >
                  <template #reference>
                    <UButton variant="ghost" color="error" icon="i-lucide-trash-2">
                      {{ deleteActionLabel(provider) }}
                    </UButton>
                  </template>
                </ConfirmAction>
              </span>
            </div>

            <div class="provider-facts">
              <span><b>Base URL</b>{{ endpointLabel(provider) }}</span>
              <span><b>密钥方式</b>{{ credentialLabel(provider) }}</span>
              <span
                ><b>可用模型</b>{{ provider.config.allowed_models.join('、') || '未填写' }}</span
              >
              <span><b>同时请求数</b>{{ provider.config.max_concurrency }}</span>
            </div>

            <div
              v-if="
                Object.keys(provider.model_bindings).length ||
                Object.keys(provider.agent_bindings).length
              "
              class="provider-routes"
            >
              <UBadge
                v-for="(model, logical) in provider.model_bindings"
                :key="`logical:${logical}`"
                size="sm"
                variant="subtle"
              >
                {{ logicalModelLabel(logical) }} → {{ model }}
              </UBadge>
              <UBadge
                v-for="(model, agent) in provider.agent_bindings"
                :key="`agent:${agent}`"
                size="sm"
                color="warning"
                variant="subtle"
              >
                {{ agentBindingLabel(agent) }} → {{ model }}
              </UBadge>
            </div>
          </article>
        </div>
      </template>

      <form v-else class="provider-form" @submit.prevent>
        <div class="provider-form-heading">
          <div>
            <strong>
              {{
                editingExisting
                  ? `编辑${form.name === 'compatible' ? '默认模型服务' : form.name}`
                  : '添加模型服务'
              }}
            </strong>
            <small>协议决定请求格式；模型名称使用当前 API Key 在上游可访问的模型 ID。</small>
          </div>
        </div>

        <section class="provider-form-section">
          <h3>连接信息</h3>
          <div class="provider-form-grid">
            <UFormField v-if="!editingExisting" label="配置名称（用于区分多个服务）">
              <UInput v-model="form.name" placeholder="例如 team-gateway" class="w-full" />
            </UFormField>
            <UFormField label="是否用于任务">
              <USwitch v-model="form.enabled" />
            </UFormField>
            <label class="native-form-field"
              ><span>API 协议</span>
              <select v-model="form.apiFamily" aria-label="API 协议">
                <option
                  v-for="protocol in protocolOptions"
                  :key="protocol.value"
                  :value="protocol.value"
                >
                  {{ protocol.label }}
                </option>
              </select>
              <small>{{ activeProtocol.description }}</small>
            </label>
            <UFormField label="接口地址（Base URL）">
              <UInput
                v-model="form.baseUrl"
                placeholder="输入域名、IP 或完整接口地址"
                class="w-full"
              />
            </UFormField>
            <UFormField
              v-if="form.credentialSource === 'stored'"
              :label="form.credentialConfigured ? '模型服务密钥（留空保持不变）' : '模型服务密钥'"
            >
              <UInput
                v-model="form.apiKey"
                type="password"
                autocomplete="new-password"
                placeholder="输入 API Key"
                @input="form.clearApiKey = false"
                class="w-full"
              />
            </UFormField>
            <template v-else>
              <UFormField
                label="接口地址变量名（可选）"
                description="设置后优先使用此环境变量中的地址。"
              >
                <UInput
                  v-model="form.baseUrlEnv"
                  placeholder="例如 TEAM_MODEL_BASE_URL"
                  class="w-full"
                />
              </UFormField>
              <UFormField label="密钥变量名">
                <UInput
                  v-model="form.apiKeyEnv"
                  placeholder="例如 TEAM_MODEL_API_KEY"
                  class="w-full"
                />
              </UFormField>
            </template>
          </div>
          <details class="provider-advanced connection-advanced">
            <summary>高级连接设置</summary>
            <div class="provider-form-grid provider-advanced-grid">
              <label v-if="form.apiFamily === 'chat_completions'" class="native-form-field"
                ><span>Chat 兼容参数</span>
                <select v-model="form.protocolVariant">
                  <option value="openai">OpenAI 标准</option>
                  <option value="deepseek">DeepSeek</option>
                </select>
              </label>
              <label class="native-form-field"
                ><span>鉴权方式</span>
                <select v-model="form.authScheme">
                  <option value="auto">跟随协议（推荐）</option>
                  <option value="bearer">Authorization: Bearer</option>
                  <option value="x_api_key">x-api-key</option>
                </select>
                <small>默认 OpenAI 使用 Bearer；Claude 使用 x-api-key。</small>
              </label>
              <label class="native-form-field"
                ><span>密钥提供方式</span
                ><select v-model="form.credentialSource">
                  <option value="stored">在本页面加密保存</option>
                  <option value="environment">由部署环境提供</option>
                </select></label
              >
              <UFormField
                v-if="form.credentialSource === 'stored' && form.credentialConfigured"
                label="已保存的密钥"
              >
                <UCheckbox v-model="form.clearApiKey" label="删除原来的密钥" />
              </UFormField>
            </div>
            <p>密钥在本页加密保存，或通过环境变量注入。</p>
          </details>
        </section>

        <section class="provider-form-section">
          <h3>模型选择</h3>
          <p>从上游获取列表，勾选允许使用的模型，再选择默认工作模型。</p>
          <ModelCatalog
            :items="upstreamModels"
            :selected="availableModels"
            :loading="discovering"
            :error="discoveryError"
            :fetched="discoveryFetched"
            :truncated="discoveryTruncated"
            @refresh="fetchModels"
            @toggle="toggleUpstreamModel"
          />
          <UFormField label="已选模型（也可手动添加 ID）">
            <UInputTags
              v-model="form.allowedModels"
              class="w-full"
              placeholder="输入模型名称后按回车"
              @update:model-value="syncAllowedModels"
            />
          </UFormField>
          <label class="native-form-field"
            ><span>默认工作模型（推荐）</span
            ><select v-model="form.defaultModel" @change="applyDefaultModel(form.defaultModel)">
              <option value="">自动继承 / 未指定</option>
              <option v-for="model in availableModels" :key="model" :value="model">
                {{ model }}
              </option>
            </select></label
          >

          <ModelReasoningSettings
            v-model="form.modelSettings"
            :models="availableModels"
            :connection="verificationConnection"
            @busy="verifyingModel = $event"
          />

          <details class="provider-advanced">
            <summary>按工作阶段分别选择模型（可选）</summary>
            <p>只有在不同阶段需要不同模型时才修改；留在默认值即可使用上面的工作模型。</p>
            <div class="binding-list">
              <label v-for="logical in logicalModels" :key="logical">
                <span
                  ><strong>{{ logicalModelLabel(logical) }}</strong></span
                >
                <select v-model="form.modelBindings[logical]">
                  <option value="">自动继承 / 未指定</option>
                  <option v-for="model in availableModels" :key="model" :value="model">
                    {{ model }}
                  </option>
                </select>
              </label>
            </div>
          </details>
        </section>

        <section class="provider-form-section">
          <details class="provider-advanced">
            <summary>为 Agent 单独选择模型（可选）</summary>
            <p>单独设置会优先于阶段默认值；不选择时会自动继承。</p>
            <div class="binding-list">
              <label v-for="target in bindTargets" :key="target.id">
                <span>
                  <strong>{{ target.displayName }}</strong>
                  <small>{{ target.description }}</small>
                </span>
                <select v-model="form.agentBindings[target.id]">
                  <option value="">自动继承 / 未指定</option>
                  <option v-for="model in availableModels" :key="model" :value="model">
                    {{ model }}
                  </option>
                </select>
              </label>
            </div>
          </details>
        </section>

        <section class="provider-form-section">
          <details class="provider-advanced">
            <summary>高级兼容与网络选项（通常不用修改）</summary>
            <div class="provider-form-grid provider-advanced-grid">
              <label class="native-form-field"
                ><span>结构化内容格式</span
                ><select v-model="form.structuredOutputMode">
                  <option value="">
                    {{ form.apiFamily === 'messages' ? 'JSON 提示词（网关兼容）' : '自动选择' }}
                  </option>
                  <option value="json_schema">严格 JSON Schema（需上游支持）</option>
                  <option value="json_object">通用 JSON 对象</option>
                </select></label
              >
              <label v-if="form.apiFamily === 'chat_completions'" class="native-form-field"
                ><span>输出长度参数</span
                ><select v-model="form.chatTokenParameter">
                  <option value="">自动选择</option>
                  <option value="max_completion_tokens">新版兼容参数</option>
                  <option value="max_tokens">传统兼容参数</option>
                </select></label
              >
              <UFormField label="请求超时（秒）">
                <UInputNumber v-model="form.timeoutSeconds" :min="1" :max="600" class="w-full" />
              </UFormField>
              <UFormField label="同时请求数">
                <UInputNumber v-model="form.maxConcurrency" :min="1" :max="256" class="w-full" />
              </UFormField>
            </div>
            <div class="provider-switches">
              <UCheckbox
                v-if="form.apiFamily === 'responses'"
                v-model="form.storeResponses"
                label="允许服务端保留 Responses 响应"
              />
            </div>
          </details>
        </section>

        <div class="provider-form-actions">
          <UButton @click="cancelEdit" color="neutral" variant="outline">取消</UButton>
          <UButton
            color="neutral"
            icon="i-lucide-circle-check"
            :loading="saving"
            :disabled="verifyingModel"
            @click="saveProvider"
          >
            保存设置
          </UButton>
        </div>
      </form>
    </div>
  </section>
</template>
