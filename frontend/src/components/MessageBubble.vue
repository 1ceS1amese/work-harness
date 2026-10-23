<script setup lang="ts">
import { computed, ref } from 'vue'

import type { AgentDescriptor, Artifact, ChatMessage, RunResult } from '../types'
import {
  ACTIVE_RUN_STATUSES,
  formatCompactTime,
  getRunStage,
  statusTagType,
  STATUS_LABELS,
} from '../utils/run'
import {
  functionLabel,
  nodeLabel,
  reviewDimensionLabel,
  subtaskTitleLabel,
  toolLabel,
} from '../utils/labels'

interface ExecutionStep {
  id: string
  title: string
  detail?: string
  running?: boolean
}

const props = defineProps<{
  message: ChatMessage
  run?: RunResult
  agents: AgentDescriptor[]
  actionBusy?: boolean
}>()

const emit = defineEmits<{
  approve: [runId: string, note: string]
  reject: [runId: string, note: string]
  cancel: [runId: string]
  details: [runId: string]
}>()

const note = ref('')
const copied = ref(false)
const stage = computed(() => (props.run ? getRunStage(props.run) : null))
const isActive = computed(
  () => props.message.status !== undefined && ACTIVE_RUN_STATUSES.includes(props.message.status),
)
function descriptor(agentId: string): AgentDescriptor | undefined {
  return props.agents.find((item) => item.id === agentId)
}

function latestArtifacts(artifacts: Artifact[]): Artifact[] {
  const latest = new Map<string, Artifact>()
  for (const artifact of artifacts) {
    const previous = latest.get(artifact.subtask_id)
    if (!previous || artifact.version >= previous.version) latest.set(artifact.subtask_id, artifact)
  }
  return [...latest.values()]
}

const executionSteps = computed<ExecutionStep[]>(() => {
  const run = props.run
  if (!run) {
    return [
      {
        id: `${props.message.id}:init`,
        title: isActive.value ? '正在运行命令 · 初始化任务工作流' : '已运行命令 · 任务初始化',
        running: isActive.value,
      },
    ]
  }

  const steps: ExecutionStep[] = []
  if (run.engine === 'blackboard') {
    const stageInfo = getRunStage(run)
    steps.push({
      id: `${run.run_id}:bb-stage`,
      title: isActive.value
        ? `正在运行 · 共享黑板 · ${stageInfo.label}`
        : `已运行步骤 · 共享黑板 · ${stageInfo.label}`,
      detail: stageInfo.description,
      running: isActive.value,
    })
    for (const [idx, call] of run.tool_calls.entries()) {
      steps.push({
        id: `${run.run_id}:tool:${idx}`,
        title: `已运行命令 · ${toolLabel(call.tool_name)}`,
        detail: call.output_summary || `耗时 ${call.duration_ms}ms`,
      })
    }
    if (run.model_calls.length > 0) {
      const models = [...new Set(run.model_calls.map((c) => c.model))].join(', ')
      steps.push({
        id: `${run.run_id}:models`,
        title: `已完成推理调用 · ${run.model_calls.length} 次（${models}）`,
        detail: run.model_calls
          .map((c) => `${nodeLabel(c.node)} · ${c.model} (${c.latency_ms}ms)`)
          .join('\n'),
      })
    }
    return steps
  }

  if (run.task) {
    const assigneeNames = run.task.assignment.assignees
      .map((id) => descriptor(id)?.display_name ?? id)
      .join('、')
    steps.push({
      id: `${run.run_id}:accepted`,
      title: assigneeNames ? `已分配协作 · ${assigneeNames}` : '已接收任务并自动编排分工',
      detail: run.task.assignment.cross_review ? '启用成员交叉复核' : '启用基础检查',
    })
  }

  if (run.plan) {
    const allocation = run.plan.subtasks
      .map((item) => {
        const member = descriptor(item.worker_role)?.display_name ?? item.worker_role
        const assignedFunction = item.assigned_function
          ? `（${functionLabel(item.assigned_function)}）`
          : ''
        return `• ${member}${assignedFunction}：${subtaskTitleLabel(item.title)}`
      })
      .join('\n')
    steps.push({
      id: `${run.run_id}:plan:${run.plan.version}`,
      title: `已生成执行计划 · ${run.plan.subtasks.length} 项子任务`,
      detail: allocation,
    })
  }

  for (const artifact of latestArtifacts(run.artifacts)) {
    const member = descriptor(artifact.producer)?.display_name ?? artifact.producer
    const subtask = run.plan?.subtasks.find((item) => item.subtask_id === artifact.subtask_id)
    const fnLabel = subtask?.assigned_function ? ` · ${functionLabel(subtask.assigned_function)}` : ''
    steps.push({
      id: `${artifact.artifact_id}:${artifact.version}`,
      title: `已执行步骤 · ${member}${fnLabel}`,
      detail: artifact.content,
    })
  }

  for (const dimension of ['comprehensive', 'fact', 'logic', 'constraint']) {
    const reviews = run.reviews.filter((item) => item.dimension === dimension)
    if (reviews.length === 0) continue
    const passed = reviews.filter((item) => item.verdict === 'pass').length
    const issues = reviews.flatMap((item) => item.issues).map((item) => item.message)
    steps.push({
      id: `${run.run_id}:review:${dimension}:${reviews.length}`,
      title: `已完成检查 · ${reviewDimensionLabel(dimension)}（${passed}/${reviews.length} 通过）`,
      detail: issues.length > 0 ? `需要留意：${issues.join('；')}` : '检查通过，未发现异常问题。',
    })
  }

  if (run.decision) {
    const actionTitles = {
      approve: '把关通过 · 整理最终结果',
      repair: '触发返工 · 继续补充修正',
      request_human: '等待人工确认',
      fail: '未能通过最终检查',
    }
    steps.push({
      id: run.decision.decision_id,
      title: actionTitles[run.decision.action],
      detail: run.decision.human_prompt || run.decision.reasons.join('；') || undefined,
    })
  }

  return steps
})

const primaryOutput = computed<string>(() => {
  const run = props.run
  if (!run) return props.message.content
  if (run.engine === 'blackboard') {
    return run.output || getRunStage(run).description
  }
  if (run.output && ['succeeded', 'partial'].includes(run.status)) {
    return run.output
  }
  if (run.decision?.action === 'request_human') {
    return run.decision.human_prompt || '这一步风险比较高，得请你确认后才能继续。'
  }
  return props.message.content
})

async function copyOutput(): Promise<void> {
  if (!primaryOutput.value || typeof navigator === 'undefined' || !navigator.clipboard) return
  try {
    await navigator.clipboard.writeText(primaryOutput.value)
    copied.value = true
    window.setTimeout(() => {
      copied.value = false
    }, 1800)
  } catch {
    // Ignore clipboard permission errors in restricted contexts.
  }
}
</script>

<template>
  <article
    v-if="message.role === 'user'"
    class="message message-user"
    :title="formatCompactTime(message.createdAt)"
  >
    <div class="message-body">
      <div class="message-content">{{ message.content }}</div>
    </div>
  </article>

  <div v-else class="agent-thread">
    <div v-if="executionSteps.length" class="codex-steps" aria-label="执行步骤记录">
      <details
        v-for="step in executionSteps"
        :key="step.id"
        class="codex-step"
        :class="{ 'is-running': step.running }"
      >
        <summary class="codex-step-summary">
          <UIcon
            :name="step.running ? 'i-lucide-loader-circle' : 'i-lucide-square-terminal'"
            class="codex-step-icon"
            :class="{ spin: step.running }"
          />
          <span class="codex-step-title">{{ step.title }}</span>
          <UIcon v-if="step.detail" name="i-lucide-chevron-down" class="codex-step-chevron" />
        </summary>
        <div v-if="step.detail" class="codex-step-detail">{{ step.detail }}</div>
      </details>
    </div>

    <article class="message message-agent">
      <div class="message-body">
        <div class="message-content codex-output">{{ primaryOutput }}</div>
      </div>
    </article>

    <section v-if="run || isActive" class="run-card group-run-summary">
      <div v-if="run && stage && isActive" class="codex-progress-row">
        <UProgress
          v-if="stage.progress !== null"
          :model-value="stage.progress"
          :color="message.status === 'failed' ? 'error' : 'primary'"
          size="xs"
          aria-label="任务进度"
        />
      </div>

      <div
        v-if="
          run && run.engine !== 'blackboard' && message.status === 'waiting_human' && message.runId
        "
        class="approval-box"
      >
        <strong>这一步需要你点头</strong>
        <p>因为风险比较高，大家先暂停了。你可以补充一句说明，再决定要不要继续。</p>
        <UInput
          v-model="note"
          maxlength="500"
          placeholder="有补充说明的话，可以写在这里"
          class="w-full"
        />
        <div>
          <UButton
            color="primary"
            :loading="actionBusy"
            @click="emit('approve', message.runId, note)"
          >
            可以，继续吧
          </UButton>
          <UButton
            :loading="actionBusy"
            @click="emit('reject', message.runId, note)"
            color="neutral"
            variant="outline"
          >
            先不要做
          </UButton>
        </div>
      </div>

      <div v-if="message.runId" class="run-actions codex-message-toolbar">
        <button
          type="button"
          class="codex-icon-action"
          :aria-label="copied ? '已复制' : '复制回复'"
          :title="copied ? '已复制' : '复制回复'"
          @click="copyOutput"
        >
          <UIcon :name="copied ? 'i-lucide-check' : 'i-lucide-copy'" />
        </button>
        <UButton
          variant="ghost"
          size="xs"
          icon="i-lucide-layers"
          @click="emit('details', message.runId)"
          color="neutral"
          >查看过程与证据</UButton
        >
        <UButton
          v-if="isActive"
          variant="ghost"
          size="xs"
          icon="i-lucide-square"
          :loading="actionBusy"
          @click="emit('cancel', message.runId)"
          color="neutral"
        >
          停止任务
        </UButton>
        <UBadge
          v-if="message.status"
          size="sm"
          :color="statusTagType(message.status)"
          variant="subtle"
          class="codex-status-badge"
        >
          {{
            run?.engine === 'blackboard' && message.status === 'waiting_human'
              ? '等待补充'
              : STATUS_LABELS[message.status]
          }}
        </UBadge>
      </div>
    </section>
  </div>
</template>
