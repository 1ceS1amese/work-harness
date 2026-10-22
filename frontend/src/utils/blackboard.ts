import type { BoardObject, ClaimType, ObjectKind } from '../types'

export const KIND_LABELS: Record<ObjectKind, string> = {
  fact: 'Fact · 事实记录', intent: 'Intent · 探索方向', hint: 'Hint · 经验提示',
}
export const CLAIM_LABELS: Record<ClaimType, string> = {
  user_statement: '用户陈述', file_content: '文件内容', tool_observation: '工具观察',
  derived: '推导结论', assessment: '评估意见', delivery: '交付记录',
}
const STATUS: Record<string, string> = {
  valid: '核验记录有效', contested: '存在争议', stale: '依据已变化', superseded: '已被替代',
  expired: '已过期', retracted: '已撤回', open: '待探索', satisfied: '已满足',
  blocked: '受阻', abandoned: '已放弃', active: '可参考', consumed: '已使用', dismissed: '已忽略',
}
export function objectStatus(object: BoardObject): string {
  const value = object.kind === 'fact' ? object.lifecycle : object.status
  return STATUS[value] ?? value
}
export function objectText(object: BoardObject): string {
  return object.kind === 'fact' ? object.statement : object.kind === 'intent' ? object.objective : object.suggestion
}
export function stopReason(reason: string): string {
  return ({ goal_satisfied: '验收条件已满足', user_cancelled: '用户取消了运行',
    timeout: '达到运行时限', activation_limit: '达到调用次数上限', no_progress: '连续探索未带来有效进展',
    budget: '已达到运行限制', blocked: '没有可执行的后续方向', input_required: '需要人工补充信息',
  } as Record<string, string>)[reason] ?? reason
}
export function displayTime(value: string): string {
  return new Date(value).toLocaleString('zh-CN', { hour12: false })
}
