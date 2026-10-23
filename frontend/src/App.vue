<script setup lang="ts">
import {
  computed,
  defineAsyncComponent,
  nextTick,
  onBeforeUnmount,
  onMounted,
  reactive,
  ref,
  shallowRef,
  watch,
} from 'vue'
import { useNotice } from './composables/useNotice'

import ChatSidebar from './components/ChatSidebar.vue'
import ComposerBox from './components/ComposerBox.vue'
import GroupChatLayout from './components/GroupChatLayout.vue'
import ComposerModelSelect from './components/ComposerModelSelect.vue'
import type { ModelChoice, ModelSelection } from './types'
import MessageBubble from './components/MessageBubble.vue'
import RunHistoryDrawer from './components/RunHistoryDrawer.vue'
import RunModeOptions from './components/RunModeOptions.vue'
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
  WorkflowCapabilities,
} from './types'
import { effectiveEngine, executionInput, legacyWorkflow, type RunMode } from './utils/admission'
import { ACTIVE_RUN_STATUSES, resultMessage } from './utils/run'
import WelcomeHero from './components/WelcomeHero.vue'

const notice = useNotice()

const RunDetailsDrawer = defineAsyncComponent(() => import('./components/RunDetailsDrawer.vue'))
const SettingsPage = defineAsyncComponent(() => import('./components/SettingsPage.vue'))
const historyOpen = ref(false)
let detailRequest: AbortController | undefined

const {
  conversations,
  activeId,
  activeConversation,
  createConversation,
  updateConversation,
  renameConversation,
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
const workflow = shallowRef<WorkflowCapabilities>(legacyWorkflow)
const runMode = ref<RunMode>('default')
const acceptanceTemplate = ref('')
const blackboardMode = computed(
  () => effectiveEngine(runMode.value, workflow.value) === 'blackboard',
)
watch(activeId, () => {
  runMode.value = 'default'
  acceptanceTemplate.value = ''
})
const mobileMenuOpen = ref(false)
const detailsOpen = ref(false)
const authDialogOpen = ref(false)
const apiKeyInput = ref('')
const apiKeyConfigured = ref(harnessApi.hasApiKey())
const authenticating = ref(false)
const selectedRunId = ref<string | null>(null)
const actionBusyRunId = ref<string | null>(null)
const backendState = ref<'checking' | 'online' | 'setup' | 'offline' | 'auth'>('checking')
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
  { id: 'analysis', display_name: '梳理思路', description: '先把需求和重点捋清楚' },
  { id: 'research', display_name: '查找资料', description: '帮你找资料、核对出处' },
  { id: 'execution', display_name: '动手完成', description: '把方案做出来并整理好' },
  { id: 'risk', display_name: '检查风险', description: '提前看看哪里可能出问题' },
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
type SettingsSection = 'models' | 'prompts' | 'runtime'
const currentPage = ref<'chat' | 'settings'>(
  window.location.pathname.startsWith('/settings') ? 'settings' : 'chat',
)
const settingsSection = ref<SettingsSection>(readSettingsSection())

const dark = ref(localStorage.getItem('work-harness:theme') !== 'light')

function readSettingsSection(): SettingsSection {
  const section = new URLSearchParams(window.location.search).get('section')
  return section === 'prompts' || section === 'runtime' ? section : 'models'
}

function openSettings(section: SettingsSection = 'models'): void {
  mobileMenuOpen.value = false
  currentPage.value = 'settings'
  settingsSection.value = section
  window.history.pushState({}, '', `/settings?section=${section}`)
}

function changeSettingsSection(section: SettingsSection): void {
  settingsSection.value = section
  window.history.replaceState({}, '', `/settings?section=${section}`)
}

function openChat(): void {
  currentPage.value = 'chat'
  window.history.pushState({}, '', '/')
}

function handleBrowserNavigation(): void {
  currentPage.value = window.location.pathname.startsWith('/settings') ? 'settings' : 'chat'
  settingsSection.value = readSettingsSection()
}
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
const modelChoices = ref<ModelChoice[]>([])
const modelSelection = computed({
  get: () => activeConversation.value.modelSelection ?? null,
  set: (value: ModelSelection | null) =>
    updateConversation(activeConversation.value.id, { modelSelection: value }),
})
const selectedRun = computed(() =>
  selectedRunId.value ? (runResults[selectedRunId.value] ?? null) : null,
)
const activeRunMessage = computed<ChatMessage | undefined>(() =>
  [...activeConversation.value.messages]
    .reverse()
    .find(
      (message) => message.runId && message.status && ACTIVE_RUN_STATUSES.includes(message.status),
    ),
)
const connectionLabel = computed(() => {
  if (backendState.value === 'online') return '系统就绪'
  if (backendState.value === 'setup') return '等待配置模型服务'
  if (backendState.value === 'auth') return '需要登录'
  if (backendState.value === 'checking') return '正在连接'
  return '服务未连接'
})

function applyTheme(): void {
  document.documentElement.dataset.theme = dark.value ? 'dark' : 'light'
  document.documentElement.classList.toggle('dark', dark.value)
  document
    .querySelector('meta[name="theme-color"]')
    ?.setAttribute('content', dark.value ? '#181818' : '#ffffff')
  localStorage.setItem('work-harness:theme', dark.value ? 'dark' : 'light')
}

function toggleTheme(): void {
  dark.value = !dark.value
  applyTheme()
}

function openHistory(): void {
  historyOpen.value = true
  mobileMenuOpen.value = false
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

function togglePinConversation(id: string): void {
  const target = conversations.value.find((item) => item.id === id)
  if (!target) return
  updateConversation(id, { pinned: !target.pinned })
}

async function scrollToLatest(): Promise<void> {
  await nextTick()
  conversationPane.value?.scrollTo({
    top: conversationPane.value.scrollHeight,
    behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth',
  })
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
  runResults[run.run_id] = run
  const location = findMessageByRun(run.run_id)
  if (!location) {
    stopStreaming(run.run_id)
    stopPolling(run.run_id)
    return
  }
  backendState.value = 'online'
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
    const [health, capabilities] = await Promise.all([
      harnessApi.health(lifecycleController.signal),
      harnessApi.capabilities(lifecycleController.signal),
    ])
    backendState.value = health.status === 'ok' ? 'online' : 'setup'
    if (capabilities.task_types.length > 0) taskTypes.value = capabilities.task_types
    if (capabilities.logical_models.length > 0) logicalModels.value = capabilities.logical_models
    if (capabilities.functions?.length > 0) agentFunctions.value = capabilities.functions
    if (capabilities.agents.length > 0) agents.value = capabilities.agents
    skills.value = capabilities.skills ?? []
    modelChoices.value = capabilities.model_choices ?? []
    workflow.value = capabilities.workflow ?? legacyWorkflow
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
    notice.warning('请输入工作台访问密钥')
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
    notice.success('身份验证成功')
    return
  }
  harnessApi.clearApiKey()
  apiKeyConfigured.value = false
  if (backendState.value === 'auth') notice.error('API Key 无效')
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
  if (runIds.length > 0) notice.success('运行状态已刷新')
}

async function sendTask(): Promise<void> {
  const goal = prompt.value.trim()
  if (!goal || sending.value) return
  if (backendState.value === 'setup') {
    openSettings('models')
    notice.warning('请先配置一个可用的模型服务，再发送任务')
    return
  }
  if (backendState.value !== 'online') {
    notice.warning('请先连接服务，再发送任务')
    return
  }
  let execution: ReturnType<typeof executionInput>
  try {
    execution = executionInput(runMode.value, acceptanceTemplate.value, workflow.value)
  } catch (error) {
    notice.warning(error instanceof Error ? error.message : '请选择任务完成条件')
    return
  }
  const selectedModel = modelSelection.value
  if (
    selectedModel &&
    !modelChoices.value.some(
      (item) => item.provider === selectedModel.provider && item.model === selectedModel.model,
    )
  ) {
    notice.warning('所选模型不可用，请在设置页重新测试，或选择跟随项目设置')
    return
  }
  const conversationId = activeConversation.value.id
  const selectedTaskType = activeConversation.value.taskType
  const selectedRiskLevel = activeConversation.value.riskLevel
  const selectedSkills = activeConversation.value.skillSelections.map(({ skill_id }) => ({
    skill_id,
    provider: null,
  }))
  const selectedFunctionsByAgent = Object.fromEntries(
    Object.entries(activeConversation.value.functionsByAgent).map(([agentId, functions]) => [
      agentId,
      functions ? [...functions] : [],
    ]),
  )
  const selectedCrossReview = activeConversation.value.crossReview
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
    notice.warning('直接或按功能分配至少需要选择一名协作成员')
    return
  }
  if (
    execution.engine === 'blackboard' &&
    effectiveAssignees.some((agentId) => (selectedFunctionsByAgent[agentId]?.length ?? 0) > 1)
  ) {
    notice.warning('黑板协作中每位成员请选择一项功能，再发送任务')
    return
  }
  addMessage(conversationId, 'user', goal)
  const assistant = addMessage(conversationId, 'assistant', '正在创建受控运行…', 'pending')
  prompt.value = ''
  sending.value = true
  await scrollToLatest()

  try {
    const accepted = await harnessApi.createRun({
      ...execution,
      ...(selectedModel ? { model_selection: { ...selectedModel } } : {}),
      goal,
      task_type: selectedTaskType,
      risk_level: selectedRiskLevel,
      assignment: {
        mode: effectiveMode,
        assignees: effectiveAssignees,
        functions_by_agent: Object.fromEntries(
          effectiveAssignees.map((agentId) => [
            agentId,
            selectedFunctionsByAgent[agentId] ?? ['execution'],
          ]),
        ),
        cross_review: execution.engine === 'blackboard' ? false : selectedCrossReview,
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
    if (error instanceof ApiError && error.status === 401) {
      backendState.value = 'auth'
    } else if (error instanceof ApiError && error.status === 503) {
      backendState.value = 'setup'
      openSettings('models')
    } else {
      backendState.value = 'offline'
    }
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
    notice.error(error instanceof Error ? error.message : '审批操作失败')
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
    notice.success('运行已停止')
  } catch (error) {
    notice.error(error instanceof Error ? error.message : '停止运行失败')
  } finally {
    actionBusyRunId.value = null
  }
}

function cancelCurrentRun(): void {
  if (activeRunMessage.value?.runId) void cancelRun(activeRunMessage.value.runId)
}

async function openRunDetails(runId: string): Promise<void> {
  historyOpen.value = false
  historyOpen.value = false
  detailRequest?.abort()
  const request = new AbortController()
  detailRequest = request
  selectedRunId.value = runId
  detailsOpen.value = true
  try {
    const run = await harnessApi.getRun(runId, request.signal)
    if (!request.signal.aborted) await applyRun(run)
  } catch (error) {
    if (!request.signal.aborted)
      notice.error(error instanceof Error ? error.message : '运行详情读取失败')
  }
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
  window.addEventListener('popstate', handleBrowserNavigation)
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
  detailRequest?.abort()
  window.removeEventListener('popstate', handleBrowserNavigation)
  lifecycleController.abort()
  for (const timer of pollTimers.values()) window.clearTimeout(timer)
  for (const close of runStreams.values()) close()
})
</script>

<template>
  <SettingsPage
    v-if="currentPage === 'settings'"
    :logical-models="logicalModels"
    :agents="agents"
    :setup-mode="backendState === 'setup'"
    :initial-section="settingsSection"
    :dark="dark"
    :connection-label="connectionLabel"
    :connection-state="backendState"
    @history="openHistory"
    @back="openChat"
    @changed="checkConnection"
    @toggle-theme="toggleTheme"
    @authenticate="openAuthDialog"
    @section-change="changeSettingsSection"
  />
  <GroupChatLayout
    v-else
    v-model:mobile-menu-open="mobileMenuOpen"
    :title="activeConversation.title"
    :connection-label="connectionLabel"
    :connection-state="backendState"
    :dark="dark"
    @history="openHistory"
    @refresh="refreshActiveConversation"
    @authenticate="openAuthDialog"
    @toggle-theme="toggleTheme"
    @settings="openSettings('models')"
    @create="showNewConversation"
    :empty="activeConversation.messages.length === 0"
  >
    <template #sidebar>
      <ChatSidebar
        :conversations="conversations"
        :active-id="activeId"
        @create="showNewConversation"
        @select="chooseConversation"
        @delete="deleteConversation"
        @pin="togglePinConversation"
        @rename="renameConversation"
        @history="openHistory"
      />
    </template>
    <template #conversation>
      <section
        ref="conversationPane"
        class="conversation"
        :aria-label="activeConversation.messages.length ? '任务消息' : '开始新任务'"
      >
        <WelcomeHero v-if="activeConversation.messages.length === 0" />
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
      <div
        v-if="backendState === 'setup' || backendState === 'offline' || backendState === 'auth'"
        class="connection-notice"
        role="status"
      >
        <UIcon name="i-lucide-circle-alert" />
        <span>{{
          backendState === 'setup'
            ? '前往设置连接模型上游，选择协议和工作模型后开始任务。'
            : backendState === 'auth'
              ? '此工作空间需要身份验证。'
              : '暂时无法连接服务。你的输入会保留。'
        }}</span>
        <UButton
          size="xs"
          color="neutral"
          variant="link"
          @click="
            backendState === 'setup'
              ? openSettings('models')
              : backendState === 'auth'
                ? openAuthDialog()
                : checkConnection()
          "
          >{{
            backendState === 'setup'
              ? '配置模型'
              : backendState === 'auth'
                ? '验证身份'
                : '重新连接'
          }}</UButton
        >
      </div>
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
        :blackboard-mode="blackboardMode"
        @send="sendTask"
        @cancel="cancelCurrentRun"
        @mention="insertMention"
      >
        <template #model>
          <ComposerModelSelect
            v-model="modelSelection"
            :choices="modelChoices"
            :disabled="sending || Boolean(activeRunMessage)"
            @settings="openSettings('models')"
          />
        </template>
        <template #options
          ><RunModeOptions
            v-model="runMode"
            v-model:template-id="acceptanceTemplate"
            :workflow="workflow"
            :disabled="sending"
            section="mode"
        /></template>
        <template #execution
          ><RunModeOptions
            v-model="runMode"
            v-model:template-id="acceptanceTemplate"
            :workflow="workflow"
            :disabled="sending"
            section="completion"
        /></template>
      </ComposerBox>
    </template>
  </GroupChatLayout>
  <RunHistoryDrawer v-model="historyOpen" @select="openRunDetails" />
  <RunDetailsDrawer
    v-if="detailsOpen"
    v-model="detailsOpen"
    :run="selectedRun"
    :agents="agents"
    @update="applyRun"
  />
  <UModal
    v-model:open="authDialogOpen"
    title="工作台访问验证"
    description="访问当前 Work Harness 工作台"
    :dismissible="backendState !== 'auth'"
  >
    <template #body>
      <p class="auth-dialog-copy">
        此密钥用于访问工作台，仅保存在当前标签页。模型服务的 Base URL 与 API Key 请在「设置 →
        模型连接」中配置。
      </p>
      <div class="auth-dialog-fields">
        <UFormField label="工作台地址（Base URL）">
          <UInput :model-value="harnessApi.baseUrl()" readonly class="w-full" />
        </UFormField>
        <UFormField label="工作台访问密钥">
          <UInput
            v-model="apiKeyInput"
            type="password"
            autocomplete="current-password"
            class="w-full"
            placeholder="输入工作台访问密钥"
            @keyup.enter="submitApiKey"
          />
        </UFormField>
      </div>
    </template>
    <template #footer
      ><UButton v-if="apiKeyConfigured" color="neutral" variant="outline" @click="clearApiKey"
        >清除密钥</UButton
      ><UButton :loading="authenticating" @click="submitApiKey">验证并连接</UButton></template
    >
  </UModal>
</template>
