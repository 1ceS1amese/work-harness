import type { RunResult, RunStatus } from '../types'

export interface RunStage {
  label: string
  description: string
  progress: number | null
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
  if (run.engine === 'blackboard') {
    const descriptions: Record<RunStatus, [string, string]> = {
      pending: ['等待启动', '黑板运行正在排队。'],
      running: ['围绕目标探索', '知识源根据已提交的信息继续探索；可在黑板中查看验收缺口与证据。'],
      waiting_human: ['等待补充', '需要人工补充信息。查看目标缺口与受阻的探索方向。'],
      succeeded: ['验收已满足', '目标验收条件已满足，可查看对应事实及核验范围。'],
      partial: ['保留部分成果', '运行已停止，仍有验收条件未满足。'],
      failed: ['运行已停止', '未能满足目标，请查看停止原因与尚缺信息。'],
      cancelled: ['运行已取消', '黑板已停止推进，可查看已提交的记录。'],
    }
    const [label, description] = descriptions[run.status]
    return { label, description, progress: null }
  }
  if (run.status === 'succeeded') {
    return { label: '都搞定了', description: '大家已经把结果整理好了。', progress: 100 }
  }
  if (run.status === 'partial') {
    return { label: '先完成了一部分', description: '能做的部分已经整理好了，还有些问题需要留意。', progress: 100 }
  }
  if (run.status === 'failed') {
    return { label: '这次没做完', description: '处理时碰到了问题，可以看看详细记录。', progress: 100 }
  }
  if (run.status === 'cancelled') {
    return { label: '已经停下来了', description: '大家已经停止处理这件事。', progress: 100 }
  }
  if (run.status === 'waiting_human') {
    return { label: '等你确认', description: '这一步风险比较高，大家先等你点头。', progress: 90 }
  }
  if (!run.task) {
    return { label: '正在看你的消息', description: '先把你想做的事和需要注意的地方捋清楚…', progress: 10 }
  }
  if (!run.plan) {
    return { label: '正在安排分工', description: '任务管家正在拆分事情，再叫合适的成员来做…', progress: 24 }
  }

  const subtaskTotal = Math.max(run.plan.subtasks.length, 1)
  const completedSubtasks = new Set(run.artifacts.map((item) => item.subtask_id)).size
  if (completedSubtasks < subtaskTotal) {
    const progress = 32 + Math.round((completedSubtasks / subtaskTotal) * 26)
    return {
      label: '大家正在处理',
      description: `已经完成 ${completedSubtasks}/${subtaskTotal} 份分工…`,
      progress,
    }
  }

  const deepReview =
    ['high', 'critical'].includes(run.task?.risk_level ?? 'low') ||
    ['research', 'decision'].includes(run.task?.task_type ?? 'general')
  const reviewDimensions = run.task?.assignment.cross_review === false ? 1 : deepReview ? 3 : 1
  const expectedReviews = subtaskTotal * reviewDimensions
  const latestReviews = new Set(
    run.reviews.map((item) => `${item.artifact_id}:${item.artifact_version}:${item.dimension}`),
  ).size
  if (latestReviews < expectedReviews) {
    const progress = 60 + Math.round((latestReviews / expectedReviews) * 20)
    return {
      label: '大家正在互相检查',
      description: `已经检查了 ${latestReviews}/${expectedReviews} 项…`,
      progress,
    }
  }
  if (!run.decision) {
    return { label: '正在做最后检查', description: '看看能不能直接交付，还是需要再补一补…', progress: 84 }
  }
  return { label: '正在整理回复', description: '把大家的结果合在一起，马上发给你…', progress: 94 }
}

export function resultMessage(run: RunResult): string {
  if (run.status === 'succeeded' || run.status === 'partial') {
    return run.output || '运行已结束，但没有返回文本结果。'
  }
  if (run.status === 'waiting_human') {
    if (run.engine === 'blackboard') return run.output || getRunStage(run).description
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
