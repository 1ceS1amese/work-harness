import { describe, expect, it } from 'vitest'

import type { Conversation } from '../types'
import { BrowserConversationStorage } from './conversationStorage'

class FakeStorage implements Storage {
  private readonly values = new Map<string, string>()

  get length(): number {
    return this.values.size
  }

  clear(): void {
    this.values.clear()
  }

  getItem(key: string): string | null {
    return this.values.get(key) ?? null
  }

  key(index: number): string | null {
    return [...this.values.keys()][index] ?? null
  }

  removeItem(key: string): void {
    this.values.delete(key)
  }

  setItem(key: string, value: string): void {
    this.values.set(key, value)
  }
}

describe('conversation storage adapter', () => {
  it('saves and restores conversations without exposing storage calls to the composable', () => {
    const adapter = new BrowserConversationStorage(new FakeStorage(), 'conversations', 'active')
    const conversation = {
      id: 'conversation_1',
      title: '持久化设计',
      taskType: 'general',
      riskLevel: 'low',
      allocationMode: 'auto',
      selectedAgentIds: ['worker-01'],
      functionsByAgent: { 'worker-01': ['execution'] },
      crossReview: true,
      skillSelections: [],
      messages: [],
      createdAt: '2026-08-18T00:00:00Z',
      updatedAt: '2026-08-18T00:00:00Z',
    } satisfies Conversation

    expect(adapter.saveConversations([conversation])).toBe(true)
    expect(adapter.saveActiveId(conversation.id)).toBe(true)
    expect(adapter.loadConversations()).toEqual([conversation])
    expect(adapter.loadActiveId()).toBe(conversation.id)
  })
})
