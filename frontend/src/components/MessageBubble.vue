<script setup lang="ts">
import { computed, ref } from 'vue'
import {
  CircleCheck,
  CircleClose,
  Document,
  Loading,
  User,
} from '@element-plus/icons-vue'

import type { AgentDescriptor, Artifact, ChatMessage, RunResult } from '../types'
import {
  ACTIVE_RUN_STATUSES,
  formatCompactTime,
  getRunStage,
  statusTagType,
  STATUS_LABELS,
} from '../utils/run'

interface AgentEvent {
  id: string
  agentId: string
  name: string
  specialty: string
  content: string
  tone: string
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
const stage = computed(() => (props.run ? getRunStage(props.run) : null))
const isActive = computed(
  () => props.message.status !== undefined && ACTIVE_RUN_STATUSES.includes(props.message.status),
)
const functionLabels: Record<string, string> = {
  analysis: '分析',
  research: '研究',
  execution: '执行',
  risk: '风险',
}

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

function eventFor(
  id: string,
  agentId: string,
  content: string,
  tone: string,
  fallbackName: string,
  fallbackSpecialty: string,
): AgentEvent {
  const agent = descriptor(agentId)
  return {
    id,
    agentId,
    name: agent?.display_name ?? fallbackName,
    specialty: agent?.specialty ?? fallbackSpecialty,
    content,
    tone,
  }
}

const agentEvents = computed<AgentEvent[]>(() => {
  const run = props.run
  if (!run) {
    return [
      eventFor(
        `${props.message.id}:pending`,
        'planner',
        props.message.content,
        'planner',
        '编排系统',
        '正在接收任务',
      ),
    ]
  }

  const events: AgentEvent[] = []
  if (run.task) {
    const assigneeNames = run.task.assignment.assignees
      .map((id) => descriptor(id)?.display_name ?? id)
      .join('、')
    events.push(
      eventFor(
        `${run.run_id}:accepted`,
        'planner',
        assigneeNames
          ? `已收到任务。本轮由 ${assigneeNames} 协作，${run.task.assignment.cross_review ? '启用三维交叉复核。' : '采用单维约束复核。'}`
          : '已收到任务，正在根据目标自动选择执行成员。',
        'planner',
        '编排系统',
        '自动编排',
      ),
    )
  }

  if (run.plan) {
    const allocation = run.plan.subtasks
      .map((item) => {
        const member = descriptor(item.worker_role)?.display_name ?? item.worker_role
        const assignedFunction = item.assigned_function
          ? `（${functionLabels[item.assigned_function]}）`
          : ''
        return `• ${item.title} → ${member}${assignedFunction}`
      })
      .join('\n')
    events.push(
      eventFor(
        `${run.run_id}:plan:${run.plan.version}`,
        'planner',
        `已拆分 ${run.plan.subtasks.length} 个子任务并完成分配：\n${allocation}`,
        'planner',
        '编排系统',
        '自动编排',
      ),
    )
  }

  for (const artifact of latestArtifacts(run.artifacts)) {
    const event = eventFor(
        `${artifact.artifact_id}:${artifact.version}`,
        artifact.producer,
        artifact.content,
        artifact.producer.replace('worker-', ''),
        artifact.producer,
        '子任务执行',
      )
    const subtask = run.plan?.subtasks.find((item) => item.subtask_id === artifact.subtask_id)
    event.specialty = subtask?.assigned_function
      ? `${functionLabels[subtask.assigned_function]}功能`
      : '执行功能'
    events.push(event)
  }

  const reviewLabels: Record<string, string> = {
    fact: '复核系统',
    logic: '复核系统',
    constraint: '复核系统',
  }
  for (const dimension of ['fact', 'logic', 'constraint']) {
    const reviews = run.reviews.filter((item) => item.dimension === dimension)
    if (reviews.length === 0) continue
    const passed = reviews.filter((item) => item.verdict === 'pass').length
    const issues = reviews.flatMap((item) => item.issues).map((item) => item.message)
    const details = issues.length > 0 ? `\n发现：${issues.join('；')}` : ''
    events.push(
      eventFor(
        `${run.run_id}:review:${dimension}:${reviews.length}`,
        'reviewer',
        `已独立复核 ${reviews.length} 份产物，${passed} 份通过。${details}`,
        `review-${dimension}`,
        reviewLabels[dimension] ?? '复核员',
        `${dimension} 维度复核`,
      ),
    )
    events[events.length - 1]!.name = reviewLabels[dimension] ?? '复核员'
  }

  if (run.decision) {
    const actionLabels = {
      approve: '通过质量门禁，可以进入最终汇总。',
      repair: '发现需要返工的内容，已退回目标成员处理。',
      request_human: run.decision.human_prompt || '风险较高，需要群主确认后继续。',
      fail: '未通过质量门禁，本轮运行结束。',
    }
    events.push(
      eventFor(
        run.decision.decision_id,
        'judge',
        actionLabels[run.decision.action],
        'judge',
        '质量门禁',
        '质量门禁',
      ),
    )
  }

  if (run.output && ['succeeded', 'partial'].includes(run.status)) {
    events.push(
      eventFor(
        `${run.run_id}:output`,
        'synthesizer',
        run.output,
        'synthesizer',
        '汇总系统',
        '最终成果',
      ),
    )
  }
  return events
})
</script>

<template>
  <article v-if="message.role === 'user'" class="message message-user">
    <div class="message-avatar"><User /></div>
    <div class="message-body">
      <div class="message-meta"><strong>你 · 群主</strong></div>
      <div class="message-content">{{ message.content }}</div>
    </div>
  </article>

  <div v-else class="agent-thread">
    <article
      v-for="event in agentEvents"
      :key="event.id"
      class="message message-agent"
      :class="`agent-tone-${event.tone}`"
    >
      <div class="message-avatar agent-message-avatar">
        {{ event.name.slice(0, 1) }}
      </div>
      <div class="message-body">
        <div class="message-meta">
          <strong>{{ event.name }}</strong>
          <span class="agent-specialty">{{ event.specialty }}</span>
          <small>{{ formatCompactTime(message.createdAt) }}</small>
        </div>
        <div class="message-content">{{ event.content }}</div>
      </div>
    </article>

    <section v-if="run || isActive" class="run-card group-run-summary">
      <template v-if="run && stage">
        <div class="run-card-heading">
          <span>
            <Loading v-if="isActive" class="spin" />
            <CircleCheck v-else-if="message.status === 'succeeded'" />
            <CircleClose v-else-if="message.status === 'failed'" />
            <Document v-else />
            协作进度 · {{ stage.label }}
          </span>
          <el-tag
            v-if="message.status"
            size="small"
            round
            effect="plain"
            :type="statusTagType(message.status)"
          >
            {{ STATUS_LABELS[message.status] }}
          </el-tag>
        </div>
        <el-progress
          :percentage="stage.progress"
          :stroke-width="6"
          :show-text="false"
          :status="message.status === 'failed' ? 'exception' : message.status === 'succeeded' ? 'success' : undefined"
        />
        <div class="run-facts">
          <span>{{ run.plan?.subtasks.length ?? 0 }} 个分派</span>
          <span>{{ run.artifacts.length }} 条成员产出</span>
          <span>{{ run.reviews.length }} 次交叉复核</span>
        </div>
      </template>
      <div v-else class="run-placeholder">
        <Loading class="spin" /> 正在创建协作群任务…
      </div>

      <div v-if="message.status === 'waiting_human' && message.runId" class="approval-box">
        <strong>质量门禁请求群主决定</strong>
        <p>高风险步骤已暂停。你可以附上说明，再批准或拒绝。</p>
        <el-input v-model="note" maxlength="500" placeholder="可选：补充审批说明" />
        <div>
          <el-button
            type="primary"
            :loading="actionBusy"
            @click="emit('approve', message.runId, note)"
          >
            批准并继续
          </el-button>
          <el-button :loading="actionBusy" @click="emit('reject', message.runId, note)">
            拒绝
          </el-button>
        </div>
      </div>

      <div v-if="message.runId" class="run-actions">
        <el-button text size="small" @click="emit('details', message.runId)">查看协作链路</el-button>
        <el-button
          v-if="isActive"
          text
          size="small"
          :loading="actionBusy"
          @click="emit('cancel', message.runId)"
        >
          停止运行
        </el-button>
      </div>
    </section>
  </div>
</template>
