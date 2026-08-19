import type { AgentFunction } from '../types'

const TASK_TYPE_LABELS: Record<string, string> = {
  general: '日常任务',
  planning: '安排计划',
  decision: '帮我做选择',
  research: '查资料',
  code: '写代码',
  safety: '安全检查',
}

const FUNCTION_COPY: Record<AgentFunction, { label: string; description: string }> = {
  analysis: {
    label: '梳理思路',
    description: '先把需求、重点和前后关系捋清楚',
  },
  research: {
    label: '查找资料',
    description: '帮你找资料、核对出处，再把有用的信息挑出来',
  },
  execution: {
    label: '动手完成',
    description: '把想法真正做出来，并整理成可以直接使用的结果',
  },
  risk: {
    label: '检查风险',
    description: '提前看看哪里可能出问题，顺手给出规避办法',
  },
}

const SKILL_COPY: Record<string, { label: string; description: string }> = {
  'repository-analysis': {
    label: '项目资料分析',
    description: '读一遍项目里的资料和代码，再给你一份有出处的结论。',
  },
  'risk-audit': {
    label: '风险检查',
    description: '从安全、稳定性和交付角度再帮你把一遍关。',
  },
}

const NODE_LABELS: Record<string, string> = {
  normalize: '读懂你的需求',
  plan: '安排分工',
  worker: '成员处理任务',
  schedule: '协调先后顺序',
  validate: '检查结果是否完整',
  reviewer: '互相检查',
  judge: '做最后把关',
  repair_plan: '安排补充修改',
  human_gate: '等你确认',
  synthesize: '整理最终回复',
  fail: '结束未完成任务',
}

const REVIEW_DIMENSION_LABELS: Record<string, string> = {
  comprehensive: '综合检查',
  fact: '事实核对',
  logic: '思路检查',
  constraint: '要求检查',
}

const REVIEW_VERDICT_LABELS: Record<string, string> = {
  pass: '没问题',
  revise: '需要补充',
  reject: '暂不通过',
}

const SUBTASK_TITLE_LABELS: Record<string, string> = {
  'Analyze the request': '先把需求捋清楚',
  'Draft an actionable result': '整理一份能直接使用的结果',
}

const TOOL_LABELS: Record<string, string> = {
  echo_read: '内容回显',
  workspace_read: '读取项目资料',
}

const LOGICAL_MODEL_LABELS: Record<string, string> = {
  'planner-strong': '安排分工（细致）',
  'worker-fast': '动手处理（快速）',
  'reviewer-independent': '独立检查',
  'judge-stable': '最后把关（稳妥）',
  'synthesizer-general': '整理回复',
}

export function taskTypeLabel(value: string): string {
  return TASK_TYPE_LABELS[value] ?? value
}

export function functionLabel(value: string): string {
  return FUNCTION_COPY[value as AgentFunction]?.label ?? value
}

export function functionDescription(value: string, fallback = ''): string {
  return FUNCTION_COPY[value as AgentFunction]?.description ?? fallback
}

export function skillLabel(value: string): string {
  return SKILL_COPY[value]?.label ?? value
}

export function skillDescription(value: string, fallback: string): string {
  return SKILL_COPY[value]?.description ?? fallback
}

export function nodeLabel(value: string): string {
  return NODE_LABELS[value] ?? value
}

export function reviewDimensionLabel(value: string): string {
  return REVIEW_DIMENSION_LABELS[value] ?? value
}

export function reviewVerdictLabel(value: string): string {
  return REVIEW_VERDICT_LABELS[value] ?? value
}

export function subtaskTitleLabel(value: string): string {
  return SUBTASK_TITLE_LABELS[value] ?? value
}

export function toolLabel(value: string): string {
  return TOOL_LABELS[value] ?? value
}

export function logicalModelLabel(value: string): string {
  return LOGICAL_MODEL_LABELS[value] ?? value
}
