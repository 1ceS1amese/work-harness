import type { TaskRequest, WorkflowCapabilities } from '../types'

export type RunMode = 'default' | 'legacy' | 'blackboard'

export const legacyWorkflow: WorkflowCapabilities = {
  default_engine: 'legacy',
  accept_new_blackboard_runs: false,
  blackboard_templates: [],
}

export function effectiveEngine(mode: RunMode, workflow: WorkflowCapabilities) {
  return mode === 'default' ? workflow.default_engine : mode
}

export function executionInput(
  mode: RunMode,
  templateId: string,
  workflow: WorkflowCapabilities,
): Pick<TaskRequest, 'engine' | 'blackboard'> {
  const engine = effectiveEngine(mode, workflow)
  if (engine === 'legacy') return { engine }
  if (!workflow.accept_new_blackboard_runs) {
    throw new Error('当前已停止接收新的黑板任务，请选择按计划执行。')
  }
  const template = workflow.blackboard_templates.find((item) => item.id === templateId)
  if (!template) throw new Error('请先选择黑板任务的完成条件。')
  // Pin the visible engine and copy the user-selected criteria before the request starts.
  return { engine, blackboard: JSON.parse(JSON.stringify(template.input)) as TaskRequest['blackboard'] }
}
