import type { BlackboardFact, BlackboardHint, BlackboardIntent, BlackboardObjectPage, BlackboardSummary, RunResult } from '../types'

const metadata = {
  board_id: 'board', schema_version: 1, revision: 1, created_at: '2026-10-22T00:00:00Z',
  created_by: 'worker-01', origin_activation_id: null, content_digest: 'a'.repeat(64),
  topic: 'draft', tags: [], derived_from: [], links: [], attachments: [], scope: {},
}

export const fact: BlackboardFact = {
  ...metadata, id: 'fact-1', kind: 'fact', statement: '草稿内容已保存', claim_type: 'delivery',
  claim_key: 'draft.content', value: true, lifecycle: 'valid', valid_from: null, valid_until: null,
  scope: { verification: 'stored_content_only' }, premise_refs: [],
  attachments: [{ content_digest: 'b'.repeat(64), media_type: 'text/markdown', size_bytes: 32 }],
  evidence: [{ source: 'delivery', locator: 'draft-1', content_digest: 'b'.repeat(64), description: '已核对保存内容的摘要' }],
  verification: { receipt_id: 'receipt-1', verifier_id: 'stored-draft-verifier', verifier_version: '1',
    method: 'content-digest', verified_at: metadata.created_at, content_digest: metadata.content_digest,
    checks: ['content_stored'], premise_refs: [] },
}

export const intent: BlackboardIntent = {
  ...metadata, id: 'root', kind: 'intent', objective: '保存草稿并确认适用地区', root_intent_ref: null,
  parent_intent_ref: null, priority: 100, status: 'open', satisfied_by: [], blockers: [], status_reason: '',
  acceptance: [
    { id: 'draft', description: '草稿已保存', claim_key: 'draft.content', claim_type: 'delivery',
      scope: { verification: 'stored_content_only' }, operator: 'exists', expected: null, required_checks: ['content_stored'] },
    { id: 'region', description: '用户确认适用地区', claim_key: 'region', claim_type: 'user_statement',
      scope: {}, operator: 'exists', expected: null, required_checks: ['user_confirmed'] },
  ],
}

export const hint: BlackboardHint = {
  ...metadata, id: 'hint-1', kind: 'hint', suggestion: '可以先比较地区差异', applies_when: '缺少地区信息',
  rationale: '地区影响方案', source_refs: [], confidence: 1, expires_at: null, status: 'active', status_reason: '',
}

export const summary: BlackboardSummary = {
  board_id: 'board', revision: 7, evaluated_at: metadata.created_at, counts: { fact: 1, intent: 1, hint: 1 },
  root_intent: intent, goal: { satisfied: false, witnesses: [{ object_id: fact.id, revision: 1 }], missing_criteria: ['region'], conflicts: [] },
  finalization: null, objects_cursor: 'objects-7', event_cursor: 'events-7',
}

export const objectPage: BlackboardObjectPage = {
  board_id: 'board', revision: 7, filters: { kind: null, status: null, topic: null, intent_id: null },
  items: [fact, hint, intent], total: 3, limit: 20, next_cursor: null,
}

export const run: RunResult = {
  run_id: 'board', engine: 'blackboard', status: 'waiting_human', pending_intents: [{ object_id: 'root', revision: 1 }],
  output: '请确认适用地区', task: null, plan: null, artifacts: [], reviews: [], decision: null,
  errors: [], repair_rounds: 0, usage: { input_tokens: 100, output_tokens: 50, cached_tokens: 0, estimated_cost_usd: 0.002 },
  model_calls: [], tool_calls: [],
}
