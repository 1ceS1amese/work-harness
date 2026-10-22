import { describe, expect, it } from 'vitest'

import { normalizeConversationHistory } from './useConversations'

describe('conversation history normalization', () => {
  it('drops structurally corrupt records instead of crashing during startup', () => {
    expect(normalizeConversationHistory([
      { id: 'broken', messages: { not: 'an array' } },
      null,
      'invalid',
    ])).toEqual([])
  })

  it('migrates legacy member ids and their function assignments together', () => {
    const [conversation] = normalizeConversationHistory([{
      id: 'legacy-conversation',
      title: '旧对话',
      selectedAgentIds: ['worker-analysis', 'worker-risk'],
      functionsByAgent: {
        'worker-analysis': ['analysis'],
        'worker-risk': ['risk'],
      },
      messages: [
        {
          id: 'valid-message',
          role: 'assistant',
          content: '已完成',
          status: 'succeeded',
          createdAt: '2026-08-20T00:00:00.000Z',
        },
        { id: 'broken-message', role: 'system', content: 'ignore me' },
      ],
      createdAt: '2026-08-20T00:00:00.000Z',
      updatedAt: '2026-08-20T00:00:00.000Z',
    }])

    expect(conversation?.selectedAgentIds).toEqual(['worker-01', 'worker-04'])
    expect(conversation?.functionsByAgent).toEqual({
      'worker-01': ['analysis'],
      'worker-04': ['risk'],
    })
    expect(conversation?.messages).toHaveLength(1)
    expect(conversation?.messages[0]?.status).toBe('succeeded')
  })

  it('bounds restored history to the supported conversation limit', () => {
    const restored = normalizeConversationHistory(
      Array.from({ length: 40 }, (_, index) => ({ id: `conversation-${index}`, messages: [] })),
    )

    expect(restored).toHaveLength(30)
  })
})
