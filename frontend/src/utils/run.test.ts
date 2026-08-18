import { describe, expect, it } from 'vitest'

import type { RunResult } from '../types'
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
  it('maps an unnormalized run to the first stage', () => {
    expect(getRunStage(makeRun())).toEqual({
      label: '理解任务',
      description: '正在规范化目标与约束…',
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
