import type { RunResult, RunStatus } from '../types'

export interface RunStage {
  label: string
  description: string
  progress: number
}

export const ACTIVE_RUN_STATUSES: RunStatus[] = ['pending', 'running']
export const TERMINAL_RUN_STATUSES: RunStatus[] = [
  'succeeded',
  'partial',
  'failed',
  'cancelled',
]

export const STATUS_LABELS: Record<RunStatus, string> = {
  pending: '排队中',
  running: '执行中',
  waiting_human: '等待确认',
  succeeded: '已完成',
  partial: '部分完成',
  failed: '失败',
  cancelled: '已取消',
}

export function getRunStage(run: RunResult): RunStage {
  if (run.status === 'succeeded') {
    return { label: '已完成', description: '结果已汇总，可以查看运行详情。', progress: 100 }
  }
  if (run.status === 'partial') {
    return { label: '部分完成', description: '部分结果已汇总，请检查未解决项。', progress: 100 }
  }
  if (run.status === 'failed') {
    return { label: '运行失败', description: '执行未完成，请在详情中查看错误。', progress: 100 }
  }
  if (run.status === 'cancelled') {
    return { label: '已取消', description: '运行已安全停止。', progress: 100 }
  }
  if (run.status === 'waiting_human') {
    return { label: '等待你的确认', description: '高风险步骤已暂停，确认后才会继续。', progress: 90 }
  }
  if (!run.task) {
    return { label: '理解任务', description: '正在规范化目标与约束…', progress: 10 }
  }
  if (!run.plan) {
    return { label: '制定计划', description: '正在拆分任务并安排执行顺序…', progress: 24 }
  }

  const subtaskTotal = Math.max(run.plan.subtasks.length, 1)
  const completedSubtasks = new Set(run.artifacts.map((item) => item.subtask_id)).size
  if (completedSubtasks < subtaskTotal) {
    const progress = 32 + Math.round((completedSubtasks / subtaskTotal) * 26)
    return {
      label: '并行执行',
      description: `已完成 ${completedSubtasks}/${subtaskTotal} 个子任务…`,
      progress,
    }
  }

  const reviewDimensions = run.task?.assignment.cross_review === false ? 1 : 3
  const expectedReviews = subtaskTotal * reviewDimensions
  const latestReviews = new Set(
    run.reviews.map((item) => `${item.artifact_id}:${item.artifact_version}:${item.dimension}`),
  ).size
  if (latestReviews < expectedReviews) {
    const progress = 60 + Math.round((latestReviews / expectedReviews) * 20)
    return {
      label: '交叉复核',
      description: `已完成 ${latestReviews}/${expectedReviews} 项独立检查…`,
      progress,
    }
  }
  if (!run.decision) {
    return { label: '质量裁决', description: '正在判断通过、返工或升级人工处理…', progress: 84 }
  }
  return { label: '汇总结果', description: '正在组织最终答复与证据…', progress: 94 }
}

export function resultMessage(run: RunResult): string {
  if (run.status === 'succeeded' || run.status === 'partial') {
    return run.output || '运行已结束，但没有返回文本结果。'
  }
  if (run.status === 'waiting_human') {
    return run.decision?.human_prompt || '任务已暂停，需要你确认是否继续。'
  }
  if (run.status === 'cancelled') return '任务已取消。'
  if (run.status === 'failed') {
    return run.errors.map((item) => item.message).join('\n') || '运行失败，请查看详情。'
  }
  return getRunStage(run).description
}

export function statusTagType(status: RunStatus): 'success' | 'warning' | 'danger' | 'info' {
  if (status === 'succeeded') return 'success'
  if (status === 'waiting_human' || status === 'partial') return 'warning'
  if (status === 'failed' || status === 'cancelled') return 'danger'
  return 'info'
}

export function formatCompactTime(iso: string): string {
  const date = new Date(iso)
  return new Intl.DateTimeFormat('zh-CN', {
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).format(date)
}
