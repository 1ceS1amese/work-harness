<script setup lang="ts">
import { computed, ref } from 'vue'
import {
  CircleCheck,
  CircleClose,
  Document,
  Loading,
} from '@element-plus/icons-vue'

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
  reviewDimensionLabel,
  subtaskTitleLabel,
} from '../utils/labels'

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
        '任务管家',
        '正在看你的消息',
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
          ? `收到，我先来安排一下。这次请 ${assigneeNames} 一起做。${run.task.assignment.cross_review ? '做完后，我会让大家再互相检查一遍。' : '做完后，我会进行一轮基本检查。'}`
          : '收到。我先看看这件事怎么分工，再叫合适的成员来处理。',
        'planner',
        '任务管家',
        '负责安排分工',
      ),
    )
  }

  if (run.plan) {
    const allocation = run.plan.subtasks
      .map((item) => {
        const member = descriptor(item.worker_role)?.display_name ?? item.worker_role
        const assignedFunction = item.assigned_function
          ? `，重点负责${functionLabel(item.assigned_function)}`
          : ''
        return `• ${member}：${subtaskTitleLabel(item.title)}${assignedFunction}`
      })
      .join('\n')
    events.push(
      eventFor(
        `${run.run_id}:plan:${run.plan.version}`,
        'planner',
        `我把这件事分成了 ${run.plan.subtasks.length} 部分，大家这样配合：\n${allocation}`,
        'planner',
        '任务管家',
        '已经安排好了',
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
      '正在处理分到的事情',
    )
    const subtask = run.plan?.subtasks.find((item) => item.subtask_id === artifact.subtask_id)
    event.specialty = subtask?.assigned_function
      ? `这次负责${functionLabel(subtask.assigned_function)}`
      : '已经处理完成'
    events.push(event)
  }

  const reviewLabels: Record<string, string> = {
    comprehensive: '复核助手',
    fact: '事实核对助手',
    logic: '思路检查助手',
    constraint: '要求检查助手',
  }
  for (const dimension of ['comprehensive', 'fact', 'logic', 'constraint']) {
    const reviews = run.reviews.filter((item) => item.dimension === dimension)
    if (reviews.length === 0) continue
    const passed = reviews.filter((item) => item.verdict === 'pass').length
    const issues = reviews.flatMap((item) => item.issues).map((item) => item.message)
    const details = issues.length > 0 ? `\n需要留意：${issues.join('；')}` : ''
    events.push(
      eventFor(
        `${run.run_id}:review:${dimension}:${reviews.length}`,
        'reviewer',
        passed === reviews.length
          ? `我检查了 ${reviews.length} 份结果，目前都没发现问题。${details}`
          : `我检查了 ${reviews.length} 份结果，其中 ${passed} 份没问题。${details}`,
        `review-${dimension}`,
        reviewLabels[dimension] ?? '复核助手',
        reviewDimensionLabel(dimension),
      ),
    )
    events[events.length - 1]!.name = reviewLabels[dimension] ?? '复核助手'
  }

  if (run.decision) {
    const actionLabels = {
      approve: '最后一遍也检查完了，没发现需要拦住的问题。我这就整理最终回复。',
      repair: '有些地方还得补一补，我已经请对应成员继续修改。',
      request_human: run.decision.human_prompt || '这一步风险比较高，得请你确认后才能继续。',
      fail: '这次没能通过最后检查，任务先停在这里。',
    }
    events.push(
      eventFor(
        run.decision.decision_id,
        'judge',
        actionLabels[run.decision.action],
        'judge',
        '把关助手',
        '负责最后检查',
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
        '总结助手',
        '整理好的最终回复',
      ),
    )
  }
  return events
})
</script>

<template>
  <article v-if="message.role === 'user'" class="message message-user">
    <div class="message-body">
      <div class="message-meta">
        <small>{{ formatCompactTime(message.createdAt) }}</small>
        <strong>你</strong>
      </div>
      <div class="message-content">{{ message.content }}</div>
    </div>
    <div class="message-avatar user-message-avatar">我</div>
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
            任务进度 · {{ stage.label }}
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
          <span>{{ run.plan?.subtasks.length ?? 0 }} 件分工</span>
          <span>{{ run.artifacts.length }} 份成员回复</span>
          <span>{{ run.reviews.length }} 次检查</span>
        </div>
      </template>
      <div v-else class="run-placeholder">
        <Loading class="spin" /> 任务管家正在安排这件事…
      </div>

      <div v-if="message.status === 'waiting_human' && message.runId" class="approval-box">
        <strong>这一步需要你点头</strong>
        <p>因为风险比较高，大家先暂停了。你可以补充一句说明，再决定要不要继续。</p>
        <el-input v-model="note" maxlength="500" placeholder="有补充说明的话，可以写在这里" />
        <div>
          <el-button
            type="primary"
            :loading="actionBusy"
            @click="emit('approve', message.runId, note)"
          >
            可以，继续吧
          </el-button>
          <el-button :loading="actionBusy" @click="emit('reject', message.runId, note)">
            先不要做
          </el-button>
        </div>
      </div>

      <div v-if="message.runId" class="run-actions">
        <el-button text size="small" @click="emit('details', message.runId)">看看大家怎么做的</el-button>
        <el-button
          v-if="isActive"
          text
          size="small"
          :loading="actionBusy"
          @click="emit('cancel', message.runId)"
        >
          先停一下
        </el-button>
      </div>
    </section>
  </div>
</template>
