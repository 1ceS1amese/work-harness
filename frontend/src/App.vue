<script setup lang="ts">
import {
  computed,
  defineAsyncComponent,
  nextTick,
  onBeforeUnmount,
  onMounted,
  reactive,
  ref,
  watch,
} from 'vue'
import { Connection, EditPen, Lock, Refresh, Setting } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'

import ChatSidebar from './components/ChatSidebar.vue'
import ComposerBox from './components/ComposerBox.vue'
import GroupChatLayout from './components/GroupChatLayout.vue'
import MessageBubble from './components/MessageBubble.vue'
import RunDetailsDrawer from './components/RunDetailsDrawer.vue'
import { useConversations } from './composables/useConversations'
import { ApiError, harnessApi } from './services/api'
import type {
  AgentDescriptor,
  AgentFunction,
  AgentFunctionDescriptor,
  AllocationMode,
  ChatMessage,
  RiskLevel,
  RunResult,
  SkillDescriptor,
  SkillSelection,
} from './types'
import { ACTIVE_RUN_STATUSES, resultMessage } from './utils/run'

const ProviderSettingsDrawer = defineAsyncComponent(
  () => import('./components/ProviderSettingsDrawer.vue'),
)

const {
  conversations,
  activeId,
  activeConversation,
  createConversation,
  selectConversation,
  deleteConversation,
  addMessage,
  patchMessage,
  setRunStatus,
  setDefaults,
  setCollaboration,
  setSkillSelections,
} = useConversations()

const prompt = ref('')
const sending = ref(false)
const mobileMenuOpen = ref(false)
const detailsOpen = ref(false)
const providerSettingsOpen = ref(false)
const authDialogOpen = ref(false)
const apiKeyInput = ref('')
const apiKeyConfigured = ref(harnessApi.hasApiKey())
const authenticating = ref(false)
const selectedRunId = ref<string | null>(null)
const actionBusyRunId = ref<string | null>(null)
const backendState = ref<'checking' | 'online' | 'offline' | 'auth'>('checking')
const taskTypes = ref(['general', 'planning', 'decision', 'research', 'code'])
const logicalModels = ref([
  'planner-strong',
  'worker-fast',
  'reviewer-independent',
  'judge-stable',
  'synthesizer-general',
])
const skills = ref<SkillDescriptor[]>([])
const agentFunctions = ref<AgentFunctionDescriptor[]>([
  { id: 'analysis', display_name: '分析', description: '梳理需求与比较方案' },
  { id: 'research', display_name: '研究', description: '收集资料与可追溯证据' },
  { id: 'execution', display_name: '执行', description: '实现方案并形成结果' },
  { id: 'risk', display_name: '风险', description: '识别安全、合规与交付风险' },
])
const agents = ref<AgentDescriptor[]>([
  {
    id: 'worker-01',
    display_name: '智能成员一',
    responsibility: '按本轮功能执行子任务',
    specialty: '按任务选择功能',
    assignable: true,
    model_route: null,
  },
  {
    id: 'worker-02',
    display_name: '智能成员二',
    responsibility: '按本轮功能执行子任务',
    specialty: '按任务选择功能',
    assignable: true,
    model_route: null,
  },
  {
    id: 'worker-03',
    display_name: '智能成员三',
    responsibility: '按本轮功能执行子任务',
    specialty: '按任务选择功能',
    assignable: true,
    model_route: null,
  },
  {
    id: 'worker-04',
    display_name: '智能成员四',
    responsibility: '按本轮功能执行子任务',
    specialty: '按任务选择功能',
    assignable: true,
    model_route: null,
  },
])
const conversationPane = ref<HTMLElement | null>(null)
const runResults = reactive<Record<string, RunResult>>({})
const pollTimers = new Map<string, number>()
const pollFailures = new Map<string, number>()
const runStreams = new Map<string, () => void>()
const lifecycleController = new AbortController()

const dark = ref(localStorage.getItem('work-harness:theme') === 'dark')
const taskType = computed({
  get: () => activeConversation.value.taskType,
  set: (value: string) => setDefaults(value, activeConversation.value.riskLevel),
})
const riskLevel = computed({
  get: () => activeConversation.value.riskLevel,
  set: (value: RiskLevel) => setDefaults(activeConversation.value.taskType, value),
})
const allocationMode = computed({
  get: () => activeConversation.value.allocationMode,
  set: (value: AllocationMode) =>
    setCollaboration(
      value,
      activeConversation.value.selectedAgentIds,
      activeConversation.value.functionsByAgent,
      activeConversation.value.crossReview,
    ),
})
const selectedAgentIds = computed({
  get: () => activeConversation.value.selectedAgentIds,
  set: (value: string[]) =>
    setCollaboration(
      activeConversation.value.allocationMode,
      value,
      Object.fromEntries(
        value.map((agentId) => [
          agentId,
          activeConversation.value.functionsByAgent[agentId] ?? ['execution'],
        ]),
      ),
      activeConversation.value.crossReview,
    ),
})
const functionsByAgent = computed({
  get: () => activeConversation.value.functionsByAgent,
  set: (value: Partial<Record<string, AgentFunction[]>>) =>
    setCollaboration(
      activeConversation.value.allocationMode,
      activeConversation.value.selectedAgentIds,
      value,
      activeConversation.value.crossReview,
    ),
})
const crossReview = computed({
  get: () => activeConversation.value.crossReview,
  set: (value: boolean) =>
    setCollaboration(
      activeConversation.value.allocationMode,
      activeConversation.value.selectedAgentIds,
      activeConversation.value.functionsByAgent,
      value,
    ),
})
const skillSelections = computed({
  get: () => activeConversation.value.skillSelections,
  set: (value: SkillSelection[]) => setSkillSelections(value),
})
const groupAgents = computed(() => {
  return selectedAgentIds.value
    .map((id) => agents.value.find((agent) => agent.id === id))
    .filter((agent): agent is AgentDescriptor => Boolean(agent))
})
const selectedRun = computed(() =>
  selectedRunId.value ? runResults[selectedRunId.value] ?? null : null,
)
const activeRunMessage = computed<ChatMessage | undefined>(() =>
  [...activeConversation.value.messages]
    .reverse()
    .find(
      (message) =>
        message.runId && message.status && ACTIVE_RUN_STATUSES.includes(message.status),
    ),
)
const connectionLabel = computed(() => {
  if (backendState.value === 'online') return '系统就绪'
  if (backendState.value === 'auth') return '需要登录'
  if (backendState.value === 'checking') return '正在连接'
  return '服务未连接'
})

function applyTheme(): void {
  document.documentElement.dataset.theme = dark.value ? 'dark' : 'light'
  document.documentElement.classList.toggle('dark', dark.value)
  localStorage.setItem('work-harness:theme', dark.value ? 'dark' : 'light')
}

function toggleTheme(): void {
  dark.value = !dark.value
  applyTheme()
}

function showNewConversation(): void {
  createConversation()
  prompt.value = ''
  mobileMenuOpen.value = false
}

function chooseConversation(id: string): void {
  selectConversation(id)
  mobileMenuOpen.value = false
}

async function scrollToLatest(): Promise<void> {
  await nextTick()
  conversationPane.value?.scrollTo({ top: conversationPane.value.scrollHeight, behavior: 'smooth' })
}

function findMessageByRun(runId: string): { conversationId: string; messageId: string } | null {
  for (const conversation of conversations.value) {
    const message = conversation.messages.find((item) => item.runId === runId)
    if (message) return { conversationId: conversation.id, messageId: message.id }
  }
  return null
}

function stopPolling(runId: string): void {
  const timer = pollTimers.get(runId)
  if (timer !== undefined) window.clearTimeout(timer)
  pollTimers.delete(runId)
  pollFailures.delete(runId)
}

function stopStreaming(runId: string): void {
  runStreams.get(runId)?.()
  runStreams.delete(runId)
}

async function applyRun(run: RunResult): Promise<void> {
  const location = findMessageByRun(run.run_id)
  if (!location) {
    stopStreaming(run.run_id)
    stopPolling(run.run_id)
    return
  }
  backendState.value = 'online'
  runResults[run.run_id] = run
  setRunStatus(run.run_id, run.status, resultMessage(run))
  if (!ACTIVE_RUN_STATUSES.includes(run.status)) {
    stopStreaming(run.run_id)
    stopPolling(run.run_id)
  }
  if (activeId.value === location.conversationId) await scrollToLatest()
}

function streamRun(runId: string): void {
  if (runStreams.has(runId) || typeof EventSource === 'undefined') {
    if (typeof EventSource === 'undefined') schedulePoll(runId, 0)
    return
  }
  stopPolling(runId)
  const close = harnessApi.watchRun(
    runId,
    (run) => void applyRun(run),
    () => {
      stopStreaming(runId)
      if (findMessageByRun(runId)) schedulePoll(runId, 250)
    },
  )
  runStreams.set(runId, close)
}

function schedulePoll(runId: string, delay = 850): void {
  stopPolling(runId)
  const timer = window.setTimeout(() => void pollRun(runId), delay)
  pollTimers.set(runId, timer)
}

async function pollRun(runId: string): Promise<void> {
  const location = findMessageByRun(runId)
  if (!location) {
    stopPolling(runId)
    return
  }
  try {
    const run = await harnessApi.getRun(runId, lifecycleController.signal)
    backendState.value = 'online'
    await applyRun(run)
    pollFailures.delete(runId)
    if (ACTIVE_RUN_STATUSES.includes(run.status) && !runStreams.has(runId)) schedulePoll(runId)
    else stopPolling(runId)
    if (activeId.value === location.conversationId) await scrollToLatest()
  } catch (error) {
    if (lifecycleController.signal.aborted) return
    const failures = (pollFailures.get(runId) ?? 0) + 1
    pollFailures.set(runId, failures)
    if (error instanceof ApiError && error.status === 401) backendState.value = 'auth'
    else backendState.value = 'offline'
    if (failures <= 5) {
      schedulePoll(runId, Math.min(1_000 * 2 ** (failures - 1), 8_000))
      return
    }
    patchMessage(location.conversationId, location.messageId, {
      content: '与服务端的连接暂时中断。运行可能仍在继续，请恢复服务后点击顶部刷新。',
    })
    stopPolling(runId)
  }
}

async function checkConnection(): Promise<boolean> {
  backendState.value = 'checking'
  try {
    const [, capabilities] = await Promise.all([
      harnessApi.health(lifecycleController.signal),
      harnessApi.capabilities(lifecycleController.signal),
    ])
    backendState.value = 'online'
    if (capabilities.task_types.length > 0) taskTypes.value = capabilities.task_types
    if (capabilities.logical_models.length > 0) logicalModels.value = capabilities.logical_models
    if (capabilities.functions?.length > 0) agentFunctions.value = capabilities.functions
    if (capabilities.agents.length > 0) agents.value = capabilities.agents
    skills.value = capabilities.skills ?? []
    return true
  } catch (error) {
    if (lifecycleController.signal.aborted) return false
    backendState.value = error instanceof ApiError && error.status === 401 ? 'auth' : 'offline'
    if (backendState.value === 'auth') authDialogOpen.value = true
    return false
  }
}

function openAuthDialog(): void {
  apiKeyInput.value = ''
  authDialogOpen.value = true
}

async function submitApiKey(): Promise<void> {
  const value = apiKeyInput.value.trim()
  if (!value) {
    ElMessage.warning('请输入部署 API Key')
    return
  }
  authenticating.value = true
  harnessApi.setApiKey(value)
  apiKeyConfigured.value = true
  const connected = await checkConnection()
  authenticating.value = false
  if (connected) {
    apiKeyInput.value = ''
    authDialogOpen.value = false
    ElMessage.success('身份验证成功')
    return
  }
  harnessApi.clearApiKey()
  apiKeyConfigured.value = false
  if (backendState.value === 'auth') ElMessage.error('API Key 无效')
}

function clearApiKey(): void {
  harnessApi.clearApiKey()
  apiKeyConfigured.value = false
  apiKeyInput.value = ''
  for (const runId of runStreams.keys()) stopStreaming(runId)
  void checkConnection()
}

async function refreshActiveConversation(): Promise<void> {
  await checkConnection()
  const runIds = activeConversation.value.messages
    .map((message) => message.runId)
    .filter((runId): runId is string => Boolean(runId))
  await Promise.all(runIds.map((runId) => pollRun(runId)))
  if (runIds.length > 0) ElMessage.success('运行状态已刷新')
}

async function sendTask(): Promise<void> {
  const goal = prompt.value.trim()
  if (!goal || sending.value) return
  const conversationId = activeConversation.value.id
  const selectedTaskType = activeConversation.value.taskType
  const selectedRiskLevel = activeConversation.value.riskLevel
  const selectedSkills = activeConversation.value.skillSelections.map((item) => ({ ...item }))
  const mentionedAgents = agents.value.filter(
    (agent) =>
      agent.assignable &&
      (goal.includes(`@${agent.display_name}`) || goal.includes(`@${agent.id}`)),
  )
  const effectiveMode: AllocationMode =
    mentionedAgents.length > 0 ? 'direct' : activeConversation.value.allocationMode
  const effectiveAssignees =
    mentionedAgents.length > 0
      ? mentionedAgents.map((agent) => agent.id)
      : activeConversation.value.selectedAgentIds
  if (effectiveMode !== 'auto' && effectiveAssignees.length === 0) {
    ElMessage.warning('直接或按功能分配至少需要选择一名协作成员')
    return
  }
  addMessage(conversationId, 'user', goal)
  const assistant = addMessage(conversationId, 'assistant', '正在创建受控运行…', 'pending')
  prompt.value = ''
  sending.value = true
  await scrollToLatest()

  try {
    const accepted = await harnessApi.createRun({
      goal,
      task_type: selectedTaskType,
      risk_level: selectedRiskLevel,
      assignment: {
        mode: effectiveMode,
        assignees: effectiveAssignees,
        functions_by_agent: Object.fromEntries(
          effectiveAssignees.map((agentId) => [
            agentId,
            activeConversation.value.functionsByAgent[agentId] ?? ['execution'],
          ]),
        ),
        cross_review: activeConversation.value.crossReview,
      },
      skills: selectedSkills,
      idempotency_key: `${conversationId}:${assistant.id}`,
    })
    patchMessage(conversationId, assistant.id, {
      runId: accepted.run_id,
      content: '正在理解任务并准备执行…',
      status: 'pending',
    })
    backendState.value = 'online'
    streamRun(accepted.run_id)
  } catch (error) {
    const message = error instanceof Error ? error.message : '无法创建运行'
    patchMessage(conversationId, assistant.id, {
      content: `任务未提交：${message}`,
      status: 'failed',
    })
    backendState.value = error instanceof ApiError && error.status === 401 ? 'auth' : 'offline'
  } finally {
    sending.value = false
    await scrollToLatest()
  }
}

async function resumeRun(runId: string, approved: boolean, note: string): Promise<void> {
  actionBusyRunId.value = runId
  try {
    await harnessApi.resumeRun(runId, approved, note)
    setRunStatus(
      runId,
      'running',
      approved ? '审批已通过，正在继续执行…' : '已拒绝该操作，正在安全结束…',
    )
    streamRun(runId)
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '审批操作失败')
  } finally {
    actionBusyRunId.value = null
  }
}

async function cancelRun(runId: string): Promise<void> {
  actionBusyRunId.value = runId
  try {
    await harnessApi.cancelRun(runId)
    stopStreaming(runId)
    stopPolling(runId)
    setRunStatus(runId, 'cancelled', '任务已取消。')
    if (runResults[runId]) runResults[runId].status = 'cancelled'
    ElMessage.success('运行已停止')
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '停止运行失败')
  } finally {
    actionBusyRunId.value = null
  }
}

function cancelCurrentRun(): void {
  if (activeRunMessage.value?.runId) void cancelRun(activeRunMessage.value.runId)
}

async function openRunDetails(runId: string): Promise<void> {
  selectedRunId.value = runId
  detailsOpen.value = true
  if (!runResults[runId]) await pollRun(runId)
}

function useSuggestion(value: string): void {
  prompt.value = value
}

function insertMention(agent: AgentDescriptor): void {
  const prefix = prompt.value && !prompt.value.endsWith(' ') ? `${prompt.value} ` : prompt.value
  const mention = `@${agent.display_name}`
  if (!prefix.includes(mention)) prompt.value = `${prefix}${mention} `
  if (!selectedAgentIds.value.includes(agent.id)) {
    selectedAgentIds.value = [...selectedAgentIds.value, agent.id]
  }
}

watch(
  () => activeConversation.value.id,
  () => {
    const runIds = activeConversation.value.messages
      .map((message) => message.runId)
      .filter((runId): runId is string => Boolean(runId))
    for (const runId of runIds) streamRun(runId)
    void scrollToLatest()
  },
)

onMounted(() => {
  applyTheme()
  void checkConnection()
  for (const conversation of conversations.value) {
    for (const message of conversation.messages) {
      if (
        message.runId &&
        message.status &&
        !['succeeded', 'partial', 'failed', 'cancelled'].includes(message.status)
      ) {
        streamRun(message.runId)
      }
    }
  }
})

onBeforeUnmount(() => {
  lifecycleController.abort()
  for (const timer of pollTimers.values()) window.clearTimeout(timer)
  for (const close of runStreams.values()) close()
})
</script>

<template>
  <GroupChatLayout
    v-model:mobile-menu-open="mobileMenuOpen"
    :title="activeConversation.title"
    :connection-label="connectionLabel"
    :connection-state="backendState"
    :members="groupAgents"
  >
    <template #sidebar>
      <ChatSidebar
        :conversations="conversations"
        :active-id="activeId"
        :dark="dark"
        @create="showNewConversation"
        @select="chooseConversation"
        @delete="deleteConversation"
        @toggle-theme="toggleTheme"
      />
    </template>

    <template #mobile-sidebar>
      <ChatSidebar
        mobile
        :conversations="conversations"
        :active-id="activeId"
        :dark="dark"
        @create="showNewConversation"
        @select="chooseConversation"
        @delete="deleteConversation"
        @toggle-theme="toggleTheme"
      />
    </template>

    <template #header-actions>
      <el-tag round effect="plain">{{ activeConversation.taskType }}</el-tag>
      <el-button
        circle
        :icon="Refresh"
        aria-label="刷新运行状态"
        @click="refreshActiveConversation"
      />
      <el-button
        circle
        :icon="Setting"
        aria-label="Provider 与模型路由"
        @click="providerSettingsOpen = true"
      />
      <el-button
        v-if="backendState === 'auth' || apiKeyConfigured"
        circle
        :icon="Lock"
        aria-label="部署 API 身份验证"
        @click="openAuthDialog"
      />
    </template>

    <template #conversation>
      <section ref="conversationPane" class="conversation">
        <div v-if="activeConversation.messages.length === 0" class="welcome-card">
          <div class="welcome-mark"><EditPen /></div>
          <p class="eyebrow"><Connection /> 多成员群聊协作</p>
          <h2>把任务发到协作群</h2>
          <p>
            使用 @成员直接指派，或让任务协调员自动拆分；执行结果会在群内交叉复核并汇总。
          </p>
          <div class="suggestions">
            <button type="button" @click="useSuggestion('制定一个可执行的项目启动计划')">
              制定一个可执行的项目计划
            </button>
            <button type="button" @click="useSuggestion('比较两个方案并给出有依据的建议')">
              比较两个方案并给出建议
            </button>
            <button type="button" @click="useSuggestion('分析这份材料并整理关键风险')">
              分析材料并整理关键风险
            </button>
          </div>
        </div>
        <div v-else class="message-list">
          <MessageBubble
            v-for="message in activeConversation.messages"
            :key="message.id"
            :message="message"
            :run="message.runId ? runResults[message.runId] : undefined"
            :agents="agents"
            :action-busy="message.runId === actionBusyRunId"
            @approve="(runId, note) => resumeRun(runId, true, note)"
            @reject="(runId, note) => resumeRun(runId, false, note)"
            @cancel="cancelRun"
            @details="openRunDetails"
          />
        </div>
      </section>
    </template>

    <template #composer>
      <ComposerBox
        v-model="prompt"
        v-model:task-type="taskType"
        v-model:risk-level="riskLevel"
        v-model:allocation-mode="allocationMode"
        v-model:selected-agent-ids="selectedAgentIds"
        v-model:functions-by-agent="functionsByAgent"
        v-model:cross-review="crossReview"
        v-model:skill-selections="skillSelections"
        :task-types="taskTypes"
        :agents="agents"
        :functions="agentFunctions"
        :sending="sending"
        :has-active-run="Boolean(activeRunMessage)"
        :skills="skills"
        @send="sendTask"
        @cancel="cancelCurrentRun"
        @mention="insertMention"
      />
    </template>

    <template #overlay>
      <RunDetailsDrawer v-model="detailsOpen" :run="selectedRun" :agents="agents" />
      <ProviderSettingsDrawer
        v-model="providerSettingsOpen"
        :logical-models="logicalModels"
        :agents="agents"
        @changed="checkConnection"
      />
      <el-dialog
        v-model="authDialogOpen"
        title="部署 API 身份验证"
        width="min(430px, calc(100vw - 32px))"
        :close-on-click-modal="backendState !== 'auth'"
      >
        <p class="auth-dialog-copy">
          输入部署环境的 WORK_HARNESS_API_KEY。密钥只保存在当前浏览器标签页的
          sessionStorage，不会写入 Provider 配置。
        </p>
        <el-input
          v-model="apiKeyInput"
          type="password"
          show-password
          autocomplete="current-password"
          placeholder="部署 API Key"
          @keyup.enter="submitApiKey"
        />
        <template #footer>
          <el-button v-if="apiKeyConfigured" @click="clearApiKey">清除密钥</el-button>
          <el-button type="primary" :loading="authenticating" @click="submitApiKey">
            验证并连接
          </el-button>
        </template>
      </el-dialog>
    </template>
  </GroupChatLayout>
</template>
