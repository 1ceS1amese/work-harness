import type { Conversation } from '../types'

const DEFAULT_CONVERSATIONS_KEY = 'work-harness:conversations:v1'
const DEFAULT_ACTIVE_KEY = 'work-harness:active-conversation:v1'

export interface ConversationStorage {
  loadConversations(): unknown
  loadActiveId(): string | null
  saveConversations(conversations: Conversation[]): boolean
  saveActiveId(activeId: string): boolean
}

export class BrowserConversationStorage implements ConversationStorage {
  constructor(
    private readonly storage?: Storage,
    private readonly conversationsKey = DEFAULT_CONVERSATIONS_KEY,
    private readonly activeKey = DEFAULT_ACTIVE_KEY,
  ) {}

  private target(): Storage {
    return this.storage ?? globalThis.localStorage
  }

  loadConversations(): unknown {
    try {
      return JSON.parse(this.target().getItem(this.conversationsKey) ?? '[]')
    } catch {
      return []
    }
  }

  loadActiveId(): string | null {
    try {
      return this.target().getItem(this.activeKey)
    } catch {
      return null
    }
  }

  saveConversations(conversations: Conversation[]): boolean {
    try {
      this.target().setItem(this.conversationsKey, JSON.stringify(conversations))
      return true
    } catch {
      return false
    }
  }

  saveActiveId(activeId: string): boolean {
    try {
      this.target().setItem(this.activeKey, activeId)
      return true
    } catch {
      return false
    }
  }
}

export const conversationStorage: ConversationStorage = new BrowserConversationStorage()
