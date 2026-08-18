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

function loadConversations(): Conversation[] {
  try {
    const parsed = conversationStorage.loadConversations()
    if (!Array.isArray(parsed)) return []
    return parsed
      .filter(
        (item): item is Conversation =>
          typeof item === 'object' && item !== null && 'id' in item && 'messages' in item,
      )
      .map((item) => ({
        ...item,
        allocationMode: item.allocationMode ?? 'cross_functional',
        selectedAgentIds: [
          ...new Set(
            (item.selectedAgentIds ?? DEFAULT_AGENT_IDS).map(
              (agentId) => LEGACY_AGENT_IDS[agentId] ?? agentId,
            ),
          ),
        ],
        functionsByAgent:
          item.functionsByAgent ??
          Object.fromEntries(
            (item.selectedAgentIds ?? DEFAULT_AGENT_IDS).map((agentId) => {
              const migratedId = LEGACY_AGENT_IDS[agentId] ?? agentId
              return [migratedId, DEFAULT_FUNCTIONS[migratedId] ?? ['execution']]
            }),
          ),
        crossReview: item.crossReview ?? true,
        skillSelections: item.skillSelections ?? [],
      }))
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
        | 'title'
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
    if (role === 'user' && conversation.title === '新对话') {
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
    addMessage,
    patchMessage,
    setRunStatus,
    setDefaults,
    setCollaboration,
    setSkillSelections,
  }
}
