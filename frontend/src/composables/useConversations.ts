import { computed, ref, watch } from 'vue'

import { conversationStorage } from '../services/conversationStorage'
import type {
  AgentFunction,
  AllocationMode,
  ChatMessage,
  Conversation,
  RiskLevel,
  RunStatus,
  SkillSelection,
} from '../types'

const MAX_CONVERSATIONS = 30
const DEFAULT_AGENT_IDS = ['worker-01', 'worker-02', 'worker-03', 'worker-04']
const DEFAULT_FUNCTIONS: Partial<Record<string, AgentFunction[]>> = {
  'worker-01': ['analysis'],
  'worker-02': ['research'],
  'worker-03': ['execution'],
  'worker-04': ['risk'],
}
const LEGACY_AGENT_IDS: Record<string, string> = {
  'worker-general': 'worker-01',
  'worker-analysis': 'worker-01',
  'worker-research': 'worker-02',
  'worker-delivery': 'worker-03',
  'worker-risk': 'worker-04',
}
const RUN_STATUSES = new Set<RunStatus>([
  'pending',
  'running',
  'waiting_human',
  'succeeded',
  'partial',
  'failed',
  'cancelled',
])
const RISK_LEVELS = new Set<RiskLevel>(['low', 'medium', 'high', 'critical'])
const ALLOCATION_MODES = new Set<AllocationMode>(['auto', 'direct', 'cross_functional'])
const AGENT_FUNCTIONS = new Set<AgentFunction>(['analysis', 'research', 'execution', 'risk'])

function makeId(prefix: string): string {
  const suffix = globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random()}`
  return `${prefix}_${suffix}`
}

function now(): string {
  return new Date().toISOString()
}

function freshConversation(): Conversation {
  const timestamp = now()
  return {
    id: makeId('conversation'),
    title: '新对话',
    taskType: 'general',
    riskLevel: 'low',
    allocationMode: 'cross_functional',
    selectedAgentIds: [...DEFAULT_AGENT_IDS],
    functionsByAgent: { ...DEFAULT_FUNCTIONS },
    crossReview: true,
    skillSelections: [],
    messages: [],
    createdAt: timestamp,
    updatedAt: timestamp,
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function normalizeMessages(value: unknown): ChatMessage[] {
  if (!Array.isArray(value)) return []
  return value.flatMap((raw): ChatMessage[] => {
    if (
      !isRecord(raw) ||
      typeof raw.id !== 'string' ||
      (raw.role !== 'user' && raw.role !== 'assistant') ||
      typeof raw.content !== 'string'
    )
      return []
    return [
      {
        id: raw.id,
        role: raw.role,
        content: raw.content,
        createdAt: typeof raw.createdAt === 'string' ? raw.createdAt : now(),
        ...(typeof raw.runId === 'string' ? { runId: raw.runId } : {}),
        ...(typeof raw.status === 'string' && RUN_STATUSES.has(raw.status as RunStatus)
          ? { status: raw.status as RunStatus }
          : {}),
      },
    ]
  })
}

function normalizeSkillSelections(value: unknown): SkillSelection[] {
  if (!Array.isArray(value)) return []
  return value.flatMap((raw): SkillSelection[] => {
    if (!isRecord(raw) || typeof raw.skill_id !== 'string') return []
    return [
      {
        skill_id: raw.skill_id,
        // Model routing is selected once per conversation in the composer.
        provider: null,
      },
    ]
  })
}

function normalizeFunctions(
  value: unknown,
  selectedAgentIds: string[],
): Partial<Record<string, AgentFunction[]>> {
  const normalized: Partial<Record<string, AgentFunction[]>> = {}
  if (isRecord(value)) {
    for (const [rawAgentId, rawFunctions] of Object.entries(value)) {
      if (!Array.isArray(rawFunctions)) continue
      const agentId = LEGACY_AGENT_IDS[rawAgentId] ?? rawAgentId
      const functions = [
        ...new Set(
          rawFunctions.filter(
            (item): item is AgentFunction =>
              typeof item === 'string' && AGENT_FUNCTIONS.has(item as AgentFunction),
          ),
        ),
      ]
      if (functions.length > 0) normalized[agentId] = functions
    }
  }
  for (const agentId of selectedAgentIds) {
    normalized[agentId] ??= [...(DEFAULT_FUNCTIONS[agentId] ?? ['execution'])]
  }
  return normalized
}

export function normalizeConversationHistory(value: unknown): Conversation[] {
  if (!Array.isArray(value)) return []
  return value
    .flatMap((raw): Conversation[] => {
      if (!isRecord(raw) || typeof raw.id !== 'string' || !Array.isArray(raw.messages)) return []
      const selectedAgentIds = [
        ...new Set(
          (Array.isArray(raw.selectedAgentIds) ? raw.selectedAgentIds : DEFAULT_AGENT_IDS)
            .filter((agentId): agentId is string => typeof agentId === 'string')
            .map((agentId) => LEGACY_AGENT_IDS[agentId] ?? agentId),
        ),
      ]
      const createdAt = typeof raw.createdAt === 'string' ? raw.createdAt : now()
      return [
        {
          id: raw.id,
          title: typeof raw.title === 'string' && raw.title ? raw.title : '新对话',
          ...(raw.titleEdited === true ? { titleEdited: true } : {}),
          ...(raw.pinned === true ? { pinned: true } : {}),
          taskType: typeof raw.taskType === 'string' && raw.taskType ? raw.taskType : 'general',
          riskLevel:
            typeof raw.riskLevel === 'string' && RISK_LEVELS.has(raw.riskLevel as RiskLevel)
              ? (raw.riskLevel as RiskLevel)
              : 'low',
          allocationMode:
            typeof raw.allocationMode === 'string' &&
            ALLOCATION_MODES.has(raw.allocationMode as AllocationMode)
              ? (raw.allocationMode as AllocationMode)
              : 'cross_functional',
          modelSelection:
            isRecord(raw.modelSelection) &&
            typeof raw.modelSelection.provider === 'string' &&
            typeof raw.modelSelection.model === 'string'
              ? { provider: raw.modelSelection.provider, model: raw.modelSelection.model }
              : null,
          selectedAgentIds,
          functionsByAgent: normalizeFunctions(raw.functionsByAgent, selectedAgentIds),
          crossReview: typeof raw.crossReview === 'boolean' ? raw.crossReview : true,
          skillSelections: normalizeSkillSelections(raw.skillSelections),
          messages: normalizeMessages(raw.messages),
          createdAt,
          updatedAt: typeof raw.updatedAt === 'string' ? raw.updatedAt : createdAt,
        },
      ]
    })
    .slice(0, MAX_CONVERSATIONS)
}

function loadConversations(): Conversation[] {
  try {
    return normalizeConversationHistory(conversationStorage.loadConversations())
  } catch {
    return []
  }
}

export function useConversations() {
  const loaded = loadConversations()
  const conversations = ref<Conversation[]>(loaded.length > 0 ? loaded : [freshConversation()])
  const savedActiveId = conversationStorage.loadActiveId()
  const activeId = ref(
    conversations.value.some((item) => item.id === savedActiveId)
      ? (savedActiveId as string)
      : conversations.value[0]!.id,
  )
  const activeConversation = computed(
    () => conversations.value.find((item) => item.id === activeId.value) ?? conversations.value[0]!,
  )

  watch(
    conversations,
    (value) => {
      conversationStorage.saveConversations(value.slice(0, MAX_CONVERSATIONS))
    },
    { deep: true },
  )
  watch(activeId, (value) => conversationStorage.saveActiveId(value))

  function createConversation(): Conversation {
    const conversation = freshConversation()
    conversations.value.unshift(conversation)
    conversations.value = conversations.value.slice(0, MAX_CONVERSATIONS)
    activeId.value = conversation.id
    return conversation
  }

  function selectConversation(id: string): void {
    if (conversations.value.some((item) => item.id === id)) activeId.value = id
  }

  function deleteConversation(id: string): void {
    conversations.value = conversations.value.filter((item) => item.id !== id)
    if (conversations.value.length === 0) conversations.value = [freshConversation()]
    if (activeId.value === id) activeId.value = conversations.value[0]!.id
  }

  function updateConversation(
    id: string,
    patch: Partial<
      Pick<
        Conversation,
        | 'modelSelection'
        | 'pinned'
        | 'title'
        | 'titleEdited'
        | 'taskType'
        | 'riskLevel'
        | 'allocationMode'
        | 'selectedAgentIds'
        | 'functionsByAgent'
        | 'crossReview'
        | 'skillSelections'
      >
    >,
  ): void {
    const conversation = conversations.value.find((item) => item.id === id)
    if (!conversation) return
    Object.assign(conversation, patch, { updatedAt: now() })
  }

  function renameConversation(id: string, title: string): void {
    const value = title.trim()
    if (!value) return
    updateConversation(id, { title: value, titleEdited: true })
  }

  function addMessage(
    conversationId: string,
    role: ChatMessage['role'],
    content: string,
    status?: RunStatus,
  ): ChatMessage {
    const message: ChatMessage = {
      id: makeId('message'),
      role,
      content,
      createdAt: now(),
      ...(status ? { status } : {}),
    }
    const conversation = conversations.value.find((item) => item.id === conversationId)
    if (!conversation) throw new Error('Conversation not found')
    conversation.messages.push(message)
    conversation.updatedAt = message.createdAt
    if (role === 'user' && !conversation.titleEdited && conversation.title === '新对话') {
      conversation.title = content.replace(/\s+/g, ' ').trim().slice(0, 28) || '新对话'
    }
    conversations.value.sort((a, b) => b.updatedAt.localeCompare(a.updatedAt))
    return message
  }

  function patchMessage(
    conversationId: string,
    messageId: string,
    patch: Partial<Pick<ChatMessage, 'content' | 'runId' | 'status'>>,
  ): void {
    const conversation = conversations.value.find((item) => item.id === conversationId)
    const message = conversation?.messages.find((item) => item.id === messageId)
    if (!conversation || !message) return
    Object.assign(message, patch)
    conversation.updatedAt = now()
  }

  function setRunStatus(runId: string, status: RunStatus, content?: string): void {
    for (const conversation of conversations.value) {
      const message = conversation.messages.find((item) => item.runId === runId)
      if (!message) continue
      message.status = status
      if (content !== undefined) message.content = content
      conversation.updatedAt = now()
      return
    }
  }

  function setDefaults(taskType: string, riskLevel: RiskLevel): void {
    updateConversation(activeConversation.value.id, { taskType, riskLevel })
  }

  function setCollaboration(
    allocationMode: AllocationMode,
    selectedAgentIds: string[],
    functionsByAgent: Partial<Record<string, AgentFunction[]>>,
    crossReview: boolean,
  ): void {
    updateConversation(activeConversation.value.id, {
      allocationMode,
      selectedAgentIds,
      functionsByAgent,
      crossReview,
    })
  }

  function setSkillSelections(skillSelections: SkillSelection[]): void {
    updateConversation(activeConversation.value.id, { skillSelections })
  }

  return {
    conversations,
    activeId,
    activeConversation,
    createConversation,
    selectConversation,
    deleteConversation,
    updateConversation,
    renameConversation,
    addMessage,
    patchMessage,
    setRunStatus,
    setDefaults,
    setCollaboration,
    setSkillSelections,
  }
}
