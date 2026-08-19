<script setup lang="ts">
import { computed } from 'vue'

import type { AgentDescriptor, RunResult } from '../types'
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

const open = defineModel<boolean>({ required: true })
const props = defineProps<{ run: RunResult | null; agents: AgentDescriptor[] }>()

const stage = computed(() => (props.run ? getRunStage(props.run) : null))
const totalLatency = computed(
  () => props.run?.model_calls.reduce((sum, item) => sum + item.latency_ms, 0) ?? 0,
)

function percent(value: number): string {
  return `${Math.round(value * 100)}%`
}

function agentName(agentId: string): string {
  return props.agents.find((item) => item.id === agentId)?.display_name ?? agentId
}

function artifactTitle(subtaskId: string): string {
  const subtask = props.run?.plan?.subtasks.find((item) => item.subtask_id === subtaskId)
  return subtask ? subtaskTitleLabel(subtask.title) : subtaskId
}
</script>

<template>
  <el-drawer v-model="open" size="min(620px, 92vw)" class="details-drawer">
    <template #header>
      <div class="drawer-title">
        <span>任务详情</span>
        <code v-if="run">{{ run.run_id }}</code>
      </div>
    </template>

    <el-empty v-if="!run" description="这次任务还没有详细记录" />
    <div v-else class="details-content">
      <section class="detail-section summary-section">
        <div>
          <p class="detail-kicker">现在进行到</p>
          <h3>{{ stage?.label }}</h3>
          <p>{{ stage?.description }}</p>
        </div>
        <el-tag round effect="plain" :type="statusTagType(run.status)">
          {{ STATUS_LABELS[run.status] }}
        </el-tag>
      </section>

      <section class="metric-grid">
        <div><span>分工数量</span><strong>{{ run.plan?.subtasks.length ?? 0 }}</strong></div>
        <div><span>AI 沟通次数</span><strong>{{ run.model_calls.length }}</strong></div>
        <div><span>使用工具次数</span><strong>{{ run.tool_calls.length }}</strong></div>
        <div><span>模型用量</span><strong>{{ run.usage.input_tokens + run.usage.output_tokens }}</strong></div>
        <div><span>模型处理时间</span><strong>{{ (totalLatency / 1000).toFixed(1) }}s</strong></div>
      </section>

      <section v-if="run.task" class="detail-section assignment-detail">
        <h3>这次怎么分工</h3>
        <div>
          <el-tag effect="plain">
            {{
              run.task.assignment.mode === 'cross_functional'
                ? '按大家擅长的事来分'
                : run.task.assignment.mode === 'direct'
                  ? '由你直接点名'
                  : '让任务管家安排'
            }}
          </el-tag>
          <span>
            {{
              run.task.assignment.assignees.map(agentName).join('、') || '由任务管家自动叫人'
            }}
          </span>
          <span v-if="Object.keys(run.task.assignment.functions_by_agent).length">
            {{
              Object.entries(run.task.assignment.functions_by_agent)
                .map(([agentId, functions]) =>
                  `${agentName(agentId)}：${functions?.map(functionLabel).join('/')}`,
                )
                .join('；')
            }}
          </span>
          <el-tag effect="plain" :type="run.task.assignment.cross_review ? 'success' : 'info'">
            {{ run.task.assignment.cross_review ? '做完后互相检查' : '只做基础检查' }}
          </el-tag>
          <el-tag
            v-for="skill in run.task.skills ?? []"
            :key="skill.skill_id"
            effect="plain"
            type="warning"
          >
            {{ skillLabel(skill.skill_id) }} · {{ skill.provider || '自动选择模型服务' }}
          </el-tag>
        </div>
      </section>

      <section v-if="run.plan" class="detail-section">
        <h3>任务管家的分工安排</h3>
        <el-timeline>
          <el-timeline-item
            v-for="subtask in run.plan.subtasks"
            :key="subtask.subtask_id"
            :timestamp="`${agentName(subtask.worker_role)}${subtask.assigned_function ? ` · ${functionLabel(subtask.assigned_function)}` : ''}`"
            placement="top"
          >
            <strong>{{ subtaskTitleLabel(subtask.title) }}</strong>
            <p>{{ subtask.description }}</p>
            <small v-if="subtask.skill_ids?.length">会用到：{{ subtask.skill_ids.map(skillLabel).join('、') }}</small>
          </el-timeline-item>
        </el-timeline>
      </section>

      <section v-if="run.artifacts.length" class="detail-section">
        <h3>群成员交回来的结果</h3>
        <el-collapse>
          <el-collapse-item
            v-for="artifact in run.artifacts"
            :key="`${artifact.artifact_id}:${artifact.version}`"
            :name="`${artifact.artifact_id}:${artifact.version}`"
          >
            <template #title>
              <span class="collapse-title">
                {{ artifactTitle(artifact.subtask_id) }} · 第 {{ artifact.version }} 版
                <el-tag size="small" effect="plain">把握度 {{ percent(artifact.confidence) }}</el-tag>
              </span>
            </template>
            <p class="artifact-content">{{ artifact.content }}</p>
            <ul v-if="artifact.unresolved_questions.length">
              <li v-for="item in artifact.unresolved_questions" :key="item">{{ item }}</li>
            </ul>
          </el-collapse-item>
        </el-collapse>
      </section>

      <section v-if="run.reviews.length" class="detail-section">
        <h3>互相检查的记录</h3>
        <div class="review-list">
          <div v-for="review in run.reviews" :key="review.review_id" class="review-item">
            <span>{{ reviewDimensionLabel(review.dimension) }}</span>
            <strong>{{ reviewVerdictLabel(review.verdict) }}</strong>
            <small>{{ review.issues.length }} 处需要留意 · 把握度 {{ percent(review.confidence) }}</small>
          </div>
        </div>
      </section>

      <section v-if="run.model_calls.length" class="detail-section">
        <h3>模型处理记录</h3>
        <el-table :data="run.model_calls" size="small">
          <el-table-column label="当时在做什么" min-width="140">
            <template #default="scope">{{ nodeLabel(scope.row.node) }}</template>
          </el-table-column>
          <el-table-column prop="model" label="模型" min-width="140" />
          <el-table-column prop="provider" label="模型服务" min-width="120" />
          <el-table-column prop="latency_ms" label="耗时(ms)" width="100" />
          <el-table-column prop="retries" label="重试" width="70" />
        </el-table>
      </section>

      <section v-if="run.tool_calls.length" class="detail-section">
        <h3>工具使用记录</h3>
        <el-table :data="run.tool_calls" size="small">
          <el-table-column label="工具" min-width="140">
            <template #default="scope">{{ toolLabel(scope.row.tool_name) }}</template>
          </el-table-column>
          <el-table-column prop="role" label="成员" min-width="120">
            <template #default="scope">{{ agentName(scope.row.role) }}</template>
          </el-table-column>
          <el-table-column prop="duration_ms" label="耗时(ms)" width="100" />
          <el-table-column prop="truncated" label="截断" width="70">
            <template #default="scope">{{ scope.row.truncated ? '是' : '否' }}</template>
          </el-table-column>
        </el-table>
      </section>

      <el-alert
        v-for="error in run.errors"
        :key="`${error.code}:${error.occurred_at}`"
        :title="error.code"
        :description="error.message"
        type="error"
        show-icon
        :closable="false"
      />
    </div>
  </el-drawer>
</template>
