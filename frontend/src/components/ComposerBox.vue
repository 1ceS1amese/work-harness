<script setup lang="ts">
import { computed } from 'vue'

import type {
  AgentDescriptor,
  AgentFunction,
  AgentFunctionDescriptor,
  AllocationMode,
  RiskLevel,
  SkillDescriptor,
  SkillSelection,
} from '../types'
import {
  functionDescription,
  functionLabel,
  skillDescription,
  skillLabel,
  taskTypeLabel,
} from '../utils/labels'

const content = defineModel<string>({ required: true })
const taskType = defineModel<string>('taskType', { required: true })
const riskLevel = defineModel<RiskLevel>('riskLevel', { required: true })
const allocationMode = defineModel<AllocationMode>('allocationMode', { required: true })
const selectedAgentIds = defineModel<string[]>('selectedAgentIds', { required: true })
const functionsByAgent = defineModel<Partial<Record<string, AgentFunction[]>>>('functionsByAgent', {
  required: true,
})
const crossReview = defineModel<boolean>('crossReview', { required: true })
const skillSelections = defineModel<SkillSelection[]>('skillSelections', { required: true })

const props = defineProps<{
  taskTypes: string[]
  agents: AgentDescriptor[]
  functions: AgentFunctionDescriptor[]
  sending: boolean
  hasActiveRun: boolean
  skills: SkillDescriptor[]
  blackboardMode?: boolean
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
  if (event.isComposing || event.shiftKey || props.sending) return
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
  if (props.blackboardMode) {
    functionsByAgent.value = { ...functionsByAgent.value, [agentId]: [functionId] }
    return
  }
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
  skillSelections.value = [...skillSelections.value, { skill_id: skill.id, provider: null }]
}
</script>

<template>
  <footer class="composer-wrap">
    <div class="composer">
      <textarea
        v-model="content"
        rows="3"
        aria-label="任务目标"
        :disabled="sending"
        placeholder="描述目标与期望结果，或用 @ 指定协作 Agent…"
        @keydown.enter="handleEnter"
      />
      <div v-if="blackboardMode" class="composer-execution"><slot name="execution" /></div>
      <div class="composer-toolbar">
        <div class="composer-options">
          <UPopover :content="{ side: 'top', align: 'start' }">
            <UButton
              icon="i-lucide-plus"
              color="neutral"
              variant="ghost"
              class="task-settings-trigger"
              aria-label="配置任务选项"
              :disabled="sending"
              >任务设置</UButton
            >
            <template #content>
              <div class="task-options-popover">
                <div class="popover-heading">
                  <strong>任务设置</strong>
                </div>
                <slot name="options" />
                <label
                  >分配方式<select v-model="allocationMode" aria-label="分配模式">
                    <option value="auto">自动安排</option>
                    <option value="direct">指定成员</option>
                    <option value="cross_functional">按功能分工</option>
                  </select></label
                >
                <label
                  >任务类型<select v-model="taskType" aria-label="任务类型">
                    <option v-for="item in taskTypes" :key="item" :value="item">
                      {{ taskTypeLabel(item) }}
                    </option>
                  </select></label
                >
                <label
                  >风险等级<select v-model="riskLevel" aria-label="风险等级">
                    <option value="low">普通</option>
                    <option value="medium">需要留意</option>
                    <option value="high">风险较高</option>
                    <option value="critical">非常敏感</option>
                  </select></label
                >
                <USwitch v-if="!blackboardMode" v-model="crossReview" label="交叉复核" />
                <details v-if="assignableAgents.length" class="task-options-section">
                  <summary>
                    协作成员 <small>{{ selectedAgents.length }} 位</small>
                  </summary>

                  <div class="agent-picker">
                    <div
                      v-for="agent in assignableAgents"
                      :key="agent.id"
                      class="agent-picker-item"
                      :class="{ selected: selectedAgentIds.includes(agent.id) }"
                    >
                      <button
                        class="agent-picker-member"
                        type="button"
                        :aria-pressed="selectedAgentIds.includes(agent.id)"
                        @click="toggleAgent(agent.id)"
                      >
                        <span>{{ agent.display_name }}</span
                        ><UIcon
                          :name="
                            selectedAgentIds.includes(agent.id)
                              ? 'i-lucide-circle-check'
                              : 'i-lucide-circle-plus'
                          "
                        />
                      </button>
                      <div v-if="selectedAgentIds.includes(agent.id)" class="agent-function-list">
                        <button
                          v-for="item in functions"
                          :key="item.id"
                          type="button"
                          :aria-pressed="functionsByAgent[agent.id]?.includes(item.id) || false"
                          :title="functionDescription(item.id, item.description)"
                          @click="toggleFunction(agent.id, item.id)"
                        >
                          {{ functionLabel(item.id) }}
                        </button>
                        <button type="button" @click="emit('mention', agent)">@ 提及</button>
                      </div>
                    </div>
                  </div>
                </details>
                <details v-if="skills.length" class="task-options-section">
                  <summary>
                    能力模块
                    <small>{{
                      skillSelections.length ? `已选 ${skillSelections.length}` : '按需选择'
                    }}</small>
                  </summary>

                  <div class="agent-picker">
                    <div v-for="skill in skills" :key="skill.id" class="agent-picker-item">
                      <button
                        class="agent-picker-member"
                        type="button"
                        :aria-pressed="Boolean(selectedSkill(skill.id))"
                        @click="toggleSkill(skill)"
                      >
                        <span
                          ><strong>{{ skillLabel(skill.id) }}</strong
                          ><small>{{ skillDescription(skill.id, skill.description) }}</small></span
                        ><UIcon
                          :name="
                            selectedSkill(skill.id)
                              ? 'i-lucide-circle-check'
                              : 'i-lucide-circle-plus'
                          "
                        />
                      </button>
                    </div>
                  </div>
                </details></div
            ></template>
          </UPopover>
        </div>
        <div class="composer-actions">
          <slot name="model" />
          <UButton
            v-if="hasActiveRun"
            icon="i-lucide-square"
            color="neutral"
            variant="outline"
            aria-label="停止当前任务"
            @click="emit('cancel')"
            >停止</UButton
          >
          <UButton
            icon="i-lucide-arrow-up"
            color="neutral"
            class="send-button"
            :loading="sending"
            :disabled="!content.trim() || sending"
            aria-label="发送任务"
            @click="emit('send')"
          />
        </div>
      </div>
    </div>
    <div class="composer-caption"><span>Enter 发送 · Shift + Enter 换行</span></div>
  </footer>
</template>
