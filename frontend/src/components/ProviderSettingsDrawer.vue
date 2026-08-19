<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import { CircleCheck, Delete, EditPen, Plus, Refresh } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'

import { harnessApi } from '../services/api'
import { logicalModelLabel } from '../utils/labels'
import type {
  AgentDescriptor,
  ProviderApiFamily,
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
  apiKeyEnv: string
  allowedModels: string[]
  structuredOutputMode: StructuredOutputMode
  chatTokenParameter: ChatTokenParameter
  storeResponses: boolean
  timeoutSeconds: number
  maxConcurrency: number
  allowInsecureHttp: boolean
  allowPrivateNetwork: boolean
  options: Record<string, unknown>
  modelBindings: Record<string, string>
  agentBindings: Record<string, string>
}

const open = defineModel<boolean>({ required: true })
const props = defineProps<{
  logicalModels: string[]
  agents: AgentDescriptor[]
}>()

const emit = defineEmits<{ changed: [] }>()
const providers = ref<ProviderDescriptor[]>([])
const loading = ref(false)
const saving = ref(false)
const editing = ref(false)
const editingExisting = ref(false)
const restartRequired = ref(false)

const form = reactive<ProviderFormState>(emptyForm())
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
    apiKeyEnv: '',
    allowedModels: [],
    structuredOutputMode: '',
    chatTokenParameter: '',
    storeResponses: false,
    timeoutSeconds: 60,
    maxConcurrency: 8,
    allowInsecureHttp: false,
    allowPrivateNetwork: false,
    options: {},
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
  replaceForm({
    name: provider.name,
    protocolVariant: config.protocol_variant,
    apiFamily: config.api_family,
    enabled: config.enabled,
    baseUrl: config.base_url ?? '',
    baseUrlEnv: config.base_url_env ?? '',
    apiKeyEnv: config.api_key_env ?? '',
    allowedModels: [...config.allowed_models],
    structuredOutputMode: config.structured_output_mode ?? '',
    chatTokenParameter: config.chat_token_parameter ?? '',
    storeResponses: config.store_responses,
    timeoutSeconds: config.timeout_seconds,
    maxConcurrency: config.max_concurrency,
    allowInsecureHttp: config.allow_insecure_http,
    allowPrivateNetwork: config.allow_private_network,
    options: { ...config.options },
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

function validateForm(): string | null {
  if (!/^[a-z][a-z0-9_-]{1,63}$/.test(form.name)) {
    return '模型服务名称需以小写字母开头，只能包含小写字母、数字、- 和 _'
  }
  if (availableModels.value.length === 0) return '至少需要一个可用模型 ID'
  if (form.enabled && !form.baseUrl.trim() && !form.baseUrlEnv.trim()) {
    return '启用模型服务前，需要配置 Base URL 或对应的环境变量名'
  }
  if (form.enabled && !form.apiKeyEnv.trim()) {
    return '启用模型服务前，需要填写 API Key 环境变量名'
  }
  const allowed = new Set(availableModels.value)
  const unavailable = [
    ...Object.values(compactBindings(form.modelBindings)),
    ...Object.values(compactBindings(form.agentBindings)),
  ].filter((model) => !allowed.has(model))
  if (unavailable.length > 0) return `路由使用了未加入白名单的模型：${unavailable.join('、')}`
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
      base_url_env: form.baseUrlEnv.trim() || null,
      api_key_env: form.apiKeyEnv.trim() || null,
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
  }
}

async function loadProviders(): Promise<void> {
  loading.value = true
  try {
    const response = await harnessApi.providers()
    providers.value = response.items
    restartRequired.value = response.restart_required
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
    ElMessage.success(`模型服务 ${form.name} 已保存`)
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
    ElMessage.success(`模型服务 ${provider.name} 的自定义配置已删除`)
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '模型服务删除失败')
  }
}

function statusLabel(provider: ProviderDescriptor): string {
  if (!provider.config.enabled) return '未启用'
  if (!provider.loaded) return '待重启'
  if (provider.endpoint_available === false) return '缺少端点'
  if (provider.credential_available === false) return '缺少密钥环境变量'
  return '已就绪'
}

function statusType(provider: ProviderDescriptor): 'success' | 'warning' | 'info' | 'danger' {
  if (!provider.config.enabled) return 'info'
  if (!provider.loaded) return 'warning'
  if (provider.endpoint_available === false || provider.credential_available === false) {
    return 'danger'
  }
  return 'success'
}

watch(open, (value) => {
  if (value) void loadProviders()
  else cancelEdit()
}, { immediate: true })
</script>

<template>
  <el-drawer v-model="open" size="min(760px, 96vw)" class="provider-drawer">
    <template #header>
      <div class="drawer-title provider-drawer-title">
        <span>模型服务与成员分配</span>
        <code>兼容 OpenAI 协议 / DeepSeek 方言</code>
      </div>
    </template>

    <div class="provider-settings">
      <el-alert
        v-if="restartRequired"
        title="已保存的配置尚未装载"
        description="请等当前任务结束后再重启 Work Harness，正在处理的任务不会中途切换模型服务。"
        type="warning"
        show-icon
        :closable="false"
      />
      <el-alert
        title="这里不保存 API Key"
        description="只填写环境变量名，密钥值由你在启动进程时配置。建议先以“未启用”保存并检查路由。"
        type="info"
        show-icon
        :closable="false"
      />

      <template v-if="!editing">
        <div class="provider-toolbar">
          <div>
            <strong>已配置的模型服务</strong>
            <small>YAML 默认值与 SQLite 覆盖会在启动时合并</small>
          </div>
          <span>
            <el-button :icon="Refresh" circle aria-label="刷新模型服务" @click="loadProviders" />
            <el-button type="primary" :icon="Plus" @click="startCreate">新建</el-button>
          </span>
        </div>

        <el-skeleton v-if="loading" :rows="5" animated />
        <el-empty v-else-if="providers.length === 0" description="还没有配置模型服务" />
        <div v-else class="provider-list">
          <article v-for="provider in providers" :key="provider.name" class="provider-card">
            <div class="provider-card-heading">
              <div>
                <span class="provider-name-row">
                  <strong>{{ provider.name }}</strong>
                  <el-tag size="small" effect="plain">{{ provider.source }}</el-tag>
                  <el-tag size="small" effect="plain" :type="statusType(provider)">
                    {{ statusLabel(provider) }}
                  </el-tag>
                </span>
                <small>
                  {{ provider.config.adapter }} · {{ provider.config.protocol_variant }} ·
                  {{ provider.config.api_family }}
                </small>
              </div>
              <span class="provider-card-actions">
                <el-button
                  v-if="provider.config.adapter === 'openai_compatible'"
                  text
                  :icon="EditPen"
                  @click="startEdit(provider)"
                >
                  编辑
                </el-button>
                <el-popconfirm
                  v-if="provider.source === 'persistent'"
                  :title="`删除 ${provider.name} 的持久化覆盖？`"
                  confirm-button-text="删除"
                  cancel-button-text="取消"
                  width="260"
                  @confirm="deleteProvider(provider)"
                >
                  <template #reference>
                    <el-button text type="danger" :icon="Delete">删除覆盖</el-button>
                  </template>
                </el-popconfirm>
              </span>
            </div>

            <div class="provider-facts">
              <span><b>端点</b>{{ provider.config.base_url || provider.config.base_url_env || '未配置' }}</span>
              <span><b>密钥变量</b>{{ provider.config.api_key_env || '未配置' }}</span>
              <span><b>模型</b>{{ provider.config.allowed_models.join('、') || '无' }}</span>
              <span><b>并发上限</b>{{ provider.config.max_concurrency }}</span>
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
                {{ logical }} → {{ model }}
              </el-tag>
              <el-tag
                v-for="(model, agent) in provider.agent_bindings"
                :key="`agent:${agent}`"
                size="small"
                effect="plain"
                type="warning"
              >
                {{ agent }} → {{ model }}
              </el-tag>
            </div>
          </article>
        </div>
      </template>

      <el-form v-else label-position="top" class="provider-form" @submit.prevent>
        <div class="provider-form-heading">
          <div>
            <strong>{{ editingExisting ? `编辑 ${form.name}` : '添加自定义模型服务' }}</strong>
            <small>OpenAI 仅表示协议类型，不限定服务商或模型名称</small>
          </div>
          <el-button text @click="cancelEdit">返回列表</el-button>
        </div>

        <section class="provider-form-section">
          <h3>基本配置</h3>
          <div class="provider-form-grid">
            <el-form-item label="模型服务名称">
              <el-input
                v-model="form.name"
                :disabled="editingExisting"
                placeholder="team-gateway"
              />
            </el-form-item>
            <el-form-item label="状态">
              <el-switch
                v-model="form.enabled"
                inline-prompt
                active-text="启用"
                inactive-text="暂存"
              />
            </el-form-item>
            <el-form-item label="协议方言">
              <el-select v-model="form.protocolVariant">
                <el-option label="OpenAI 协议" value="openai" />
                <el-option label="DeepSeek 方言" value="deepseek" />
              </el-select>
            </el-form-item>
            <el-form-item label="API 类型">
              <el-select v-model="form.apiFamily">
                <el-option label="/v1/chat/completions" value="chat_completions" />
                <el-option label="/v1/responses" value="responses" />
              </el-select>
            </el-form-item>
            <el-form-item label="Base URL">
              <el-input v-model="form.baseUrl" placeholder="https://gateway.example/v1" />
            </el-form-item>
            <el-form-item label="Base URL 环境变量名">
              <el-input v-model="form.baseUrlEnv" placeholder="TEAM_MODEL_BASE_URL" />
            </el-form-item>
            <el-form-item label="API Key 环境变量名">
              <el-input v-model="form.apiKeyEnv" placeholder="TEAM_MODEL_API_KEY" />
            </el-form-item>
            <el-form-item label="超时（秒）">
              <el-input-number v-model="form.timeoutSeconds" :min="1" :max="600" />
            </el-form-item>
            <el-form-item label="最大并发请求">
              <el-input-number v-model="form.maxConcurrency" :min="1" :max="256" />
            </el-form-item>
          </div>
          <el-form-item label="可用模型 ID">
            <el-select
              v-model="form.allowedModels"
              multiple
              filterable
              allow-create
              default-first-option
              placeholder="输入模型 ID 后回车，可添加多个"
            >
              <el-option
                v-for="model in availableModels"
                :key="model"
                :label="model"
                :value="model"
              />
            </el-select>
          </el-form-item>
        </section>

        <section class="provider-form-section">
          <h3>协议覆盖</h3>
          <div class="provider-form-grid">
            <el-form-item label="结构化输出">
              <el-select v-model="form.structuredOutputMode">
                <el-option label="按协议默认" value="" />
                <el-option label="json_schema" value="json_schema" />
                <el-option label="json_object" value="json_object" />
              </el-select>
            </el-form-item>
            <el-form-item label="Chat Token 参数">
              <el-select v-model="form.chatTokenParameter">
                <el-option label="按协议默认" value="" />
                <el-option label="max_completion_tokens" value="max_completion_tokens" />
                <el-option label="max_tokens" value="max_tokens" />
              </el-select>
            </el-form-item>
          </div>
          <div class="provider-switches">
            <el-checkbox v-model="form.storeResponses">Responses 允许服务端存储</el-checkbox>
            <el-checkbox v-model="form.allowPrivateNetwork">允许私有网络端点</el-checkbox>
            <el-checkbox v-model="form.allowInsecureHttp">允许非 HTTPS 端点</el-checkbox>
          </div>
        </section>

        <section class="provider-form-section">
          <h3>逻辑模型默认映射</h3>
          <p>告诉系统每类工作默认可以用这个服务里的哪个模型。</p>
          <div class="binding-list">
            <label v-for="logical in logicalModels" :key="logical">
              <span>
                <strong>{{ logicalModelLabel(logical) }}</strong>
                <small>{{ logical }}</small>
              </span>
              <el-select
                v-model="form.modelBindings[logical]"
                clearable
                placeholder="不映射"
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
        </section>

        <section class="provider-form-section">
          <h3>任务管家和群成员单独选模型</h3>
          <p>这里的单独设置会优先使用；如果某项能力已经点名模型服务，就按那次任务的选择来。</p>
          <div class="binding-list">
            <label v-for="target in bindTargets" :key="target.id">
              <span>
                <strong>{{ target.displayName }}</strong>
                <small>{{ target.description }}</small>
              </span>
              <el-select
                v-model="form.agentBindings[target.id]"
                clearable
                placeholder="继承逻辑候选"
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
        </section>

        <div class="provider-form-actions">
          <el-button @click="cancelEdit">取消</el-button>
          <el-button type="primary" :icon="CircleCheck" :loading="saving" @click="saveProvider">
            保存并等待重启
          </el-button>
        </div>
      </el-form>
    </div>
  </el-drawer>
</template>
