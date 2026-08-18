<script setup lang="ts">
import { computed } from 'vue'

import type { AgentDescriptor, RunResult } from '../types'
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

const functionLabels: Record<string, string> = {
  analysis: '分析',
  research: '研究',
  execution: '执行',
  risk: '风险',
}
</script>

<template>
  <el-drawer v-model="open" size="min(620px, 92vw)" class="details-drawer">
    <template #header>
      <div class="drawer-title">
        <span>运行详情</span>
        <code v-if="run">{{ run.run_id }}</code>
      </div>
    </template>

    <el-empty v-if="!run" description="暂无运行数据" />
    <div v-else class="details-content">
      <section class="detail-section summary-section">
        <div>
          <p class="detail-kicker">当前阶段</p>
          <h3>{{ stage?.label }}</h3>
          <p>{{ stage?.description }}</p>
        </div>
        <el-tag round effect="plain" :type="statusTagType(run.status)">
          {{ STATUS_LABELS[run.status] }}
        </el-tag>
      </section>

      <section class="metric-grid">
        <div><span>子任务</span><strong>{{ run.plan?.subtasks.length ?? 0 }}</strong></div>
        <div><span>模型调用</span><strong>{{ run.model_calls.length }}</strong></div>
        <div><span>工具调用</span><strong>{{ run.tool_calls.length }}</strong></div>
        <div><span>总 Token</span><strong>{{ run.usage.input_tokens + run.usage.output_tokens }}</strong></div>
        <div><span>模型耗时</span><strong>{{ (totalLatency / 1000).toFixed(1) }}s</strong></div>
      </section>

      <section v-if="run.task" class="detail-section assignment-detail">
        <h3>协作策略</h3>
        <div>
          <el-tag effect="plain">
            {{
              run.task.assignment.mode === 'cross_functional'
                ? '按功能优化'
                : run.task.assignment.mode === 'direct'
                  ? '直接指派'
                  : '智能分配'
            }}
          </el-tag>
          <span>
            {{
              run.task.assignment.assignees.map(agentName).join('、') || '由任务协调员自动选择'
            }}
          </span>
          <span v-if="Object.keys(run.task.assignment.functions_by_agent).length">
            {{
              Object.entries(run.task.assignment.functions_by_agent)
                .map(([agentId, functions]) =>
                  `${agentName(agentId)}：${functions?.map((item) => functionLabels[item]).join('/')}`,
                )
                .join('；')
            }}
          </span>
          <el-tag effect="plain" :type="run.task.assignment.cross_review ? 'success' : 'info'">
            {{ run.task.assignment.cross_review ? '三维交叉复核' : '单维复核' }}
          </el-tag>
          <el-tag
            v-for="skill in run.task.skills ?? []"
            :key="skill.skill_id"
            effect="plain"
            type="warning"
          >
            {{ skill.skill_id }} · {{ skill.provider || '自动 Provider' }}
          </el-tag>
        </div>
      </section>

      <section v-if="run.plan" class="detail-section">
        <h3>执行计划</h3>
        <el-timeline>
          <el-timeline-item
            v-for="subtask in run.plan.subtasks"
            :key="subtask.subtask_id"
            :timestamp="`${agentName(subtask.worker_role)}${subtask.assigned_function ? ` · ${functionLabels[subtask.assigned_function]}功能` : ''}`"
            placement="top"
          >
            <strong>{{ subtask.title }}</strong>
            <p>{{ subtask.description }}</p>
            <small v-if="subtask.skill_ids?.length">Skill：{{ subtask.skill_ids.join('、') }}</small>
          </el-timeline-item>
        </el-timeline>
      </section>

      <section v-if="run.artifacts.length" class="detail-section">
        <h3>执行产物</h3>
        <el-collapse>
          <el-collapse-item
            v-for="artifact in run.artifacts"
            :key="`${artifact.artifact_id}:${artifact.version}`"
            :name="`${artifact.artifact_id}:${artifact.version}`"
          >
            <template #title>
              <span class="collapse-title">
                {{ artifact.subtask_id }} · v{{ artifact.version }}
                <el-tag size="small" effect="plain">可信度 {{ percent(artifact.confidence) }}</el-tag>
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
        <h3>交叉复核</h3>
        <div class="review-list">
          <div v-for="review in run.reviews" :key="review.review_id" class="review-item">
            <span>{{ review.dimension }}</span>
            <strong>{{ review.verdict }}</strong>
            <small>{{ review.issues.length }} 个问题 · 可信度 {{ percent(review.confidence) }}</small>
          </div>
        </div>
      </section>

      <section v-if="run.model_calls.length" class="detail-section">
        <h3>模型调用</h3>
        <el-table :data="run.model_calls" size="small">
          <el-table-column prop="node" label="节点" min-width="100" />
          <el-table-column prop="model" label="模型" min-width="140" />
          <el-table-column prop="provider" label="Provider" min-width="120" />
          <el-table-column prop="latency_ms" label="耗时(ms)" width="100" />
          <el-table-column prop="retries" label="重试" width="70" />
        </el-table>
      </section>

      <section v-if="run.tool_calls.length" class="detail-section">
        <h3>工具调用</h3>
        <el-table :data="run.tool_calls" size="small">
          <el-table-column prop="tool_name" label="工具" min-width="140" />
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
