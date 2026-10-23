<script setup lang="ts">
import { computed } from 'vue'

import type { AgentDescriptor, AgentInteraction, RunResult } from '../types'
import {
  functionLabel,
  nodeLabel,
  reviewDimensionLabel,
  reviewVerdictLabel,
  skillLabel,
  subtaskTitleLabel,
  toolLabel,
} from '../utils/labels'
import { getRunStage, statusTagType, STATUS_LABELS } from '../utils/run'
import BlackboardRunDetails from './BlackboardRunDetails.vue'

const open = defineModel<boolean>({ required: true })
const props = defineProps<{ run: RunResult | null; agents: AgentDescriptor[] }>()
const emit = defineEmits<{ update: [run: RunResult] }>()

const stage = computed(() => (props.run ? getRunStage(props.run) : null))
const totalLatency = computed(
  () => props.run?.model_calls.reduce((sum, item) => sum + item.latency_ms, 0) ?? 0,
)

function percent(value: number): string {
  return `${Math.round(value * 100)}%`
}

function agentName(agentId: string): string {
  if (agentId === 'coordinator') return '任务协调组'
  return props.agents.find((item) => item.id === agentId)?.display_name ?? agentId
}

const interactionLabels: Record<AgentInteraction['kind'], string> = {
  assignment: '分派任务',
  dependency: '传递上游成果',
  repair: '反馈与返工',
  result: '提交成果',
  review: '提交复核',
}

function artifactTitle(subtaskId: string): string {
  const subtask = props.run?.plan?.subtasks.find((item) => item.subtask_id === subtaskId)
  return subtask ? subtaskTitleLabel(subtask.title) : subtaskId
}
</script>

<template>
  <USlideover
    v-model:open="open"
    title="任务详情"
    :description="run?.run_id || '当前任务的执行记录'"
    :ui="{
      content: run?.engine === 'blackboard' ? 'blackboard-overlay' : 'history-overlay',
      header: 'inspector-header',
      body: 'inspector-body',
    }"
  >
    <template #body>
      <UEmpty v-if="!run" title="这次任务还没有详细记录" />
      <template v-else-if="run.engine === 'blackboard'">
        <BlackboardRunDetails
          v-if="open"
          :key="run.run_id"
          :run="run"
          @update="emit('update', $event)"
        />
      </template>
      <div v-else class="details-content">
        <section class="detail-section summary-section">
          <div>
            <p class="detail-kicker">现在进行到</p>
            <h3>{{ stage?.label }}</h3>
            <p>{{ stage?.description }}</p>
          </div>
          <UBadge :color="statusTagType(run.status)" variant="subtle">
            {{ STATUS_LABELS[run.status] }}
          </UBadge>
        </section>

        <section class="metric-grid">
          <div>
            <span>分工数量</span><strong>{{ run.plan?.subtasks.length ?? 0 }}</strong>
          </div>
          <div>
            <span>AI 沟通次数</span><strong>{{ run.model_calls.length }}</strong>
          </div>
          <div>
            <span>使用工具次数</span><strong>{{ run.tool_calls.length }}</strong>
          </div>
          <div>
            <span>已记录 Token</span
            ><strong>{{ run.usage.input_tokens + run.usage.output_tokens }}</strong>
          </div>
          <div>
            <span>模型处理时间</span><strong>{{ (totalLatency / 1000).toFixed(1) }}s</strong>
          </div>
        </section>

        <p>
          输入 {{ run.usage.input_tokens }} · 输出 {{ run.usage.output_tokens }} · 输入中缓存命中
          {{ run.usage.cached_tokens }}
        </p>
        <p v-if="run.usage.usage_complete === false" role="status">
          部分调用用量尚未确认，以上为已知用量。
        </p>

        <section v-if="run.task" class="detail-section assignment-detail">
          <h3>这次怎么分工</h3>
          <div>
            <UBadge variant="subtle">
              {{
                run.task.assignment.mode === 'cross_functional'
                  ? '按大家擅长的事来分'
                  : run.task.assignment.mode === 'direct'
                    ? '由你直接点名'
                    : '让任务管家安排'
              }}
            </UBadge>
            <span>
              {{ run.task.assignment.assignees.map(agentName).join('、') || '由任务管家自动叫人' }}
            </span>
            <span v-if="Object.keys(run.task.assignment.functions_by_agent).length">
              {{
                Object.entries(run.task.assignment.functions_by_agent)
                  .map(
                    ([agentId, functions]) =>
                      `${agentName(agentId)}：${functions?.map(functionLabel).join('/')}`,
                  )
                  .join('；')
              }}
            </span>
            <UBadge :color="run.task.assignment.cross_review ? 'success' : 'info'" variant="subtle">
              {{ run.task.assignment.cross_review ? '做完后互相检查' : '只做基础检查' }}
            </UBadge>
            <UBadge
              v-for="skill in run.task.skills ?? []"
              :key="skill.skill_id"
              color="warning"
              variant="subtle"
            >
              {{ skillLabel(skill.skill_id) }}
              <template v-if="skill.provider"> · {{ skill.provider }}</template>
            </UBadge>
          </div>
        </section>

        <section v-if="run.plan" class="detail-section">
          <h3>任务管家的分工安排</h3>
          <ol class="task-timeline">
            <li v-for="subtask in run.plan.subtasks" :key="subtask.subtask_id">
              <small
                >{{ agentName(subtask.worker_role) }} ·
                {{
                  subtask.assigned_function ? functionLabel(subtask.assigned_function) : '协作'
                }}</small
              >
              <strong>{{ subtaskTitleLabel(subtask.title) }}</strong>
              <p>{{ subtask.description }}</p>
              <small v-if="subtask.skill_ids?.length"
                >会用到：{{ subtask.skill_ids.map(skillLabel).join('、') }}</small
              >
            </li>
          </ol>
        </section>

        <section v-if="run.agent_interactions?.length" class="detail-section">
          <h3>成员协作记录</h3>
          <div class="table-scroll">
            <table>
              <thead>
                <tr>
                  <th scope="col">协作内容</th>
                  <th scope="col">发送方 → 接收方</th>
                  <th scope="col">任务</th>
                  <th scope="col">产物版本</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="(row, index) in run.agent_interactions" :key="index">
                  <td>{{ interactionLabels[row.kind as AgentInteraction['kind']] }}</td>
                  <td>{{ agentName(row.sender) }} → {{ agentName(row.recipient) }}</td>
                  <td>{{ artifactTitle(row.subtask_id) }}</td>
                  <td>{{ row.artifact_version }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </section>

        <section v-if="run.artifacts.length" class="detail-section">
          <h3>群成员交回来的结果</h3>
          <div class="artifact-list">
            <details
              v-for="artifact in run.artifacts"
              :key="`${artifact.artifact_id}:${artifact.version}`"
              :name="`${artifact.artifact_id}:${artifact.version}`"
            >
              <summary>
                <span class="collapse-title">
                  {{ artifactTitle(artifact.subtask_id) }} · 第 {{ artifact.version }} 版
                  <UBadge size="sm" variant="subtle"
                    >把握度 {{ percent(artifact.confidence) }}</UBadge
                  >
                </span>
              </summary>
              <p class="artifact-content">{{ artifact.content }}</p>
              <ul v-if="artifact.unresolved_questions.length">
                <li v-for="item in artifact.unresolved_questions" :key="item">{{ item }}</li>
              </ul>
            </details>
          </div>
        </section>

        <section v-if="run.reviews.length" class="detail-section">
          <h3>互相检查的记录</h3>
          <div class="review-list">
            <div v-for="review in run.reviews" :key="review.review_id" class="review-item">
              <span>{{ reviewDimensionLabel(review.dimension) }}</span>
              <strong>{{ reviewVerdictLabel(review.verdict) }}</strong>
              <small
                >{{ review.issues.length }} 处需要留意 · 把握度
                {{ percent(review.confidence) }}</small
              >
            </div>
          </div>
        </section>

        <section v-if="run.model_calls.length" class="detail-section">
          <h3>模型处理记录</h3>
          <div class="table-scroll">
            <table>
              <thead>
                <tr>
                  <th scope="col">当时在做什么</th>
                  <th scope="col">模型</th>
                  <th scope="col">模型服务</th>
                  <th scope="col">耗时(ms)</th>
                  <th scope="col">重试</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="(row, index) in run.model_calls" :key="index">
                  <td>{{ nodeLabel(row.node) }}</td>
                  <td>{{ row.model }}</td>
                  <td>{{ row.provider }}</td>
                  <td>{{ row.latency_ms }}</td>
                  <td>{{ row.retries }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </section>

        <section v-if="run.tool_calls.length" class="detail-section">
          <h3>工具使用记录</h3>
          <div class="table-scroll">
            <table>
              <thead>
                <tr>
                  <th scope="col">工具</th>
                  <th scope="col">成员</th>
                  <th scope="col">耗时(ms)</th>
                  <th scope="col">截断</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="(row, index) in run.tool_calls" :key="index">
                  <td>{{ toolLabel(row.tool_name) }}</td>
                  <td>{{ agentName(row.role) }}</td>
                  <td>{{ row.duration_ms }}</td>
                  <td>{{ row.truncated ? '是' : '否' }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </section>

        <UAlert
          v-for="error in run.errors"
          :key="`${error.code}:${error.occurred_at}`"
          :title="error.code"
          :description="error.message"
          color="error"
          variant="subtle"
        />
      </div>
    </template>
  </USlideover>
</template>
