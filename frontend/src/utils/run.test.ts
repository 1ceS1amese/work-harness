import { describe, expect, it } from 'vitest'

import type { RunResult } from '../types'
import { functionLabel, logicalModelLabel, nodeLabel, taskTypeLabel } from './labels'
import { getRunStage, resultMessage } from './run'

function makeRun(overrides: Partial<RunResult> = {}): RunResult {
  return {
    run_id: 'run_test',
    status: 'running',
    output: '',
    task: null,
    plan: null,
    artifacts: [],
    reviews: [],
    decision: null,
    errors: [],
    repair_rounds: 0,
    usage: { input_tokens: 0, output_tokens: 0, cached_tokens: 0, estimated_cost_usd: 0 },
    model_calls: [],
    tool_calls: [],
    ...overrides,
  }
}

describe('run progress mapping', () => {
  it('uses conversational Chinese for internal task and agent identifiers', () => {
    expect(nodeLabel('plan')).toBe('安排分工')
    expect(functionLabel('analysis')).toBe('梳理思路')
    expect(taskTypeLabel('planning')).toBe('安排计划')
    expect(logicalModelLabel('planner-strong')).toBe('安排分工（细致）')
  })

  it('maps an unnormalized run to the first stage', () => {
    expect(getRunStage(makeRun())).toEqual({
      label: '正在看你的消息',
      description: '先把你想做的事和需要注意的地方捋清楚…',
      progress: 10,
    })
  })

  it('surfaces the human gate prompt as the assistant message', () => {
    const run = makeRun({
      status: 'waiting_human',
      decision: {
        decision_id: 'decision_1',
        action: 'request_human',
        reasons: ['high risk'],
        target_subtasks: ['subtask_1'],
        human_prompt: '确认执行写操作吗？',
        created_at: '2026-08-17T00:00:00Z',
      },
    })
    expect(getRunStage(run).progress).toBe(90)
    expect(resultMessage(run)).toBe('确认执行写操作吗？')
  })

  it('uses the final output for a successful run', () => {
    expect(resultMessage(makeRun({ status: 'succeeded', output: '完成结果' }))).toBe('完成结果')
  })
})
