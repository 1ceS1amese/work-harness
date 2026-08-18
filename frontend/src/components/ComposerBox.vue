<script setup lang="ts">
import { computed } from 'vue'
import { CircleCloseFilled, Plus, Promotion, UserFilled } from '@element-plus/icons-vue'

import type {
  AgentDescriptor,
  AgentFunction,
  AgentFunctionDescriptor,
  AllocationMode,
  RiskLevel,
  SkillDescriptor,
  SkillSelection,
} from '../types'

const content = defineModel<string>({ required: true })
const taskType = defineModel<string>('taskType', { required: true })
const riskLevel = defineModel<RiskLevel>('riskLevel', { required: true })
const allocationMode = defineModel<AllocationMode>('allocationMode', { required: true })
const selectedAgentIds = defineModel<string[]>('selectedAgentIds', { required: true })
const functionsByAgent = defineModel<Partial<Record<string, AgentFunction[]>>>(
  'functionsByAgent',
  { required: true },
)
const crossReview = defineModel<boolean>('crossReview', { required: true })
const skillSelections = defineModel<SkillSelection[]>('skillSelections', { required: true })

const props = defineProps<{
  taskTypes: string[]
  agents: AgentDescriptor[]
  functions: AgentFunctionDescriptor[]
  sending: boolean
  hasActiveRun: boolean
  skills: SkillDescriptor[]
}>()

const emit = defineEmits<{
  send: []
  cancel: []
  mention: [agent: AgentDescriptor]
}>()

const assignableAgents = computed(() => props.agents.filter((item) => item.assignable))
const selectedAgents = computed(() =>
  assignableAgents.value.filter((item) => selectedAgentIds.value.includes(item.id)),
)

function handleEnter(event: KeyboardEvent): void {
  if (event.isComposing || event.shiftKey) return
  event.preventDefault()
  emit('send')
}

function toggleAgent(agentId: string): void {
  if (selectedAgentIds.value.includes(agentId)) {
    selectedAgentIds.value = selectedAgentIds.value.filter((item) => item !== agentId)
    const nextFunctions = { ...functionsByAgent.value }
    delete nextFunctions[agentId]
    functionsByAgent.value = nextFunctions
    return
  }
  const defaultFunction = props.functions[selectedAgentIds.value.length % props.functions.length]
  selectedAgentIds.value = [...selectedAgentIds.value, agentId]
  functionsByAgent.value = {
    ...functionsByAgent.value,
    [agentId]: [defaultFunction?.id ?? 'execution'],
  }
}

function toggleFunction(agentId: string, functionId: AgentFunction): void {
  const active = functionsByAgent.value[agentId] ?? []
  const next = active.includes(functionId)
    ? active.filter((item) => item !== functionId)
    : [...active, functionId]
  if (next.length === 0) return
  functionsByAgent.value = { ...functionsByAgent.value, [agentId]: next }
}

function selectedSkill(skillId: string): SkillSelection | undefined {
  return skillSelections.value.find((item) => item.skill_id === skillId)
}

function toggleSkill(skill: SkillDescriptor): void {
  const selected = selectedSkill(skill.id)
  if (selected) {
    skillSelections.value = skillSelections.value.filter((item) => item.skill_id !== skill.id)
    return
  }
  skillSelections.value = [
    ...skillSelections.value,
    { skill_id: skill.id, provider: null },
  ]
}

function setSkillProvider(skillId: string, provider: string): void {
  skillSelections.value = skillSelections.value.map((item) =>
    item.skill_id === skillId ? { ...item, provider: provider || null } : item,
  )
}
</script>

<template>
  <footer class="composer-wrap">
    <div class="composer">
      <div class="assignment-strip">
        <span class="assignment-label"><UserFilled /> 本轮成员</span>
        <button
          v-for="agent in selectedAgents"
          :key="agent.id"
          class="mention-chip"
          type="button"
          :title="`在输入框中 @${agent.display_name}`"
          @click="emit('mention', agent)"
        >
          @{{ agent.display_name }}
        </button>
        <el-popover placement="top-start" :width="360" trigger="click">
          <template #reference>
            <button class="member-picker-trigger" type="button" aria-label="管理协作成员">
              <Plus /> 管理成员
            </button>
          </template>
          <div class="agent-picker">
            <div class="agent-picker-heading">
              <div>
                <strong>选择可指派成员</strong>
                <p>点击成员加入本轮任务；点击已选标签可快速指派。</p>
              </div>
              <el-switch
                v-model="crossReview"
                inline-prompt
                active-text="互审"
                inactive-text="单审"
                aria-label="是否启用交叉复核"
              />
            </div>
            <div
              v-for="agent in assignableAgents"
              :key="agent.id"
              class="agent-picker-item"
              :class="{ selected: selectedAgentIds.includes(agent.id) }"
            >
              <button class="agent-picker-member" type="button" @click="toggleAgent(agent.id)">
                <i>{{ agent.display_name.slice(0, 1) }}</i>
                <span>
                  <strong>{{ agent.display_name }}</strong>
                  <small>{{ agent.specialty || agent.responsibility }}</small>
                </span>
                <b>{{ selectedAgentIds.includes(agent.id) ? '已加入' : '加入' }}</b>
              </button>
              <div v-if="selectedAgentIds.includes(agent.id)" class="agent-function-list">
                <button
                  v-for="item in functions"
                  :key="item.id"
                  type="button"
                  :class="{ active: functionsByAgent[agent.id]?.includes(item.id) }"
                  :title="item.description"
                  @click="toggleFunction(agent.id, item.id)"
                >
                  {{ item.display_name }}
                </button>
              </div>
            </div>
          </div>
        </el-popover>
        <el-popover v-if="skills.length" placement="top-start" :width="390" trigger="click">
          <template #reference>
            <button class="member-picker-trigger" type="button" aria-label="选择技能和 Provider">
              技能 {{ skillSelections.length ? `(${skillSelections.length})` : '' }}
            </button>
          </template>
          <div class="agent-picker skill-picker">
            <div class="agent-picker-heading">
              <div>
                <strong>模块化技能</strong>
                <p>选择任务能力；Provider 可自动路由或手动固定。</p>
              </div>
            </div>
            <div
              v-for="skill in skills"
              :key="skill.id"
              class="agent-picker-item skill-picker-item"
              :class="{ selected: selectedSkill(skill.id) }"
            >
              <button class="agent-picker-member" type="button" @click="toggleSkill(skill)">
                <span>
                  <strong>{{ skill.id }}</strong>
                  <small>{{ skill.description }}</small>
                </span>
                <b>{{ selectedSkill(skill.id) ? '已启用' : '启用' }}</b>
              </button>
              <el-select
                v-if="selectedSkill(skill.id)"
                :model-value="selectedSkill(skill.id)?.provider ?? ''"
                size="small"
                aria-label="Skill Provider"
                @update:model-value="(value: string) => setSkillProvider(skill.id, value)"
              >
                <el-option label="自动选择 Provider" value="" />
                <el-option
                  v-for="provider in skill.available_providers"
                  :key="provider"
                  :label="provider"
                  :value="provider"
                />
              </el-select>
            </div>
          </div>
        </el-popover>
      </div>

      <textarea
        v-model="content"
        rows="2"
        aria-label="任务目标"
        :disabled="sending"
        placeholder="输入群任务，使用 @ 指派成员，Enter 发送"
        @keydown.enter="handleEnter"
      />
      <div class="composer-toolbar">
        <div class="composer-options">
          <el-select v-model="allocationMode" aria-label="分配模式" size="small">
            <el-option label="智能分配" value="auto" />
            <el-option label="直接指派" value="direct" />
            <el-option label="按功能优化" value="cross_functional" />
          </el-select>
          <el-select v-model="taskType" aria-label="任务类型" size="small">
            <el-option
              v-for="item in taskTypes"
              :key="item"
              :label="item"
              :value="item"
            />
          </el-select>
          <el-select v-model="riskLevel" aria-label="风险等级" size="small">
            <el-option label="低风险" value="low" />
            <el-option label="中风险" value="medium" />
            <el-option label="高风险" value="high" />
            <el-option label="关键风险" value="critical" />
          </el-select>
        </div>
        <div class="composer-actions">
          <el-button
            v-if="hasActiveRun"
            text
            :icon="CircleCloseFilled"
            aria-label="停止当前任务"
            @click="emit('cancel')"
          >
            停止
          </el-button>
          <span class="composer-hint">Enter 发送</span>
          <el-button
            type="primary"
            circle
            :icon="Promotion"
            :loading="sending"
            :disabled="!content.trim()"
            aria-label="发送任务"
            @click="emit('send')"
          />
        </div>
      </div>
    </div>
    <p class="disclaimer">所有成员使用同一执行设计；本轮功能只影响任务路由，产物默认进入独立交叉复核。</p>
  </footer>
</template>
