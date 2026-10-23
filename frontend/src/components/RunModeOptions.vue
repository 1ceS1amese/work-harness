<script setup lang="ts">
import { computed } from 'vue'
import type { WorkflowCapabilities } from '../types'
import { effectiveEngine, type RunMode } from '../utils/admission'

const mode = defineModel<RunMode>({ required: true })
const templateId = defineModel<string>('templateId', { required: true })
const props = withDefaults(
  defineProps<{
    workflow: WorkflowCapabilities
    disabled: boolean
    section?: 'all' | 'mode' | 'completion'
  }>(),
  { section: 'all' },
)
const blackboard = computed(() => effectiveEngine(mode.value, props.workflow) === 'blackboard')
</script>

<template>
  <div class="run-mode-options">
    <label v-if="section !== 'completion'">
      协作方式
      <select v-model="mode" :disabled="disabled" aria-label="协作方式">
        <option value="default">
          跟随项目设置（{{ workflow.default_engine === 'blackboard' ? '黑板协作' : '按计划执行' }}）
        </option>
        <option value="legacy">按计划执行</option>
        <option value="blackboard" :disabled="!workflow.accept_new_blackboard_runs">
          黑板协作
        </option>
      </select>
    </label>
    <div v-if="section !== 'mode' && blackboard" class="completion-options">
      <label
        v-if="workflow.accept_new_blackboard_runs"
        class="completion-field"
        :class="{ 'is-unselected': !templateId }"
      >
        <span class="completion-label">
          <UIcon
            :name="templateId ? 'i-lucide-circle-check' : 'i-lucide-circle-alert'"
            class="completion-label-icon"
          />
          <span>完成条件</span>
        </span>
        <select v-model="templateId" :disabled="disabled" aria-label="黑板任务完成条件">
          <option value="" disabled>请选择交付物</option>
          <option v-for="item in workflow.blackboard_templates" :key="item.id" :value="item.id">
            {{ item.title }}
          </option>
        </select>
      </label>
      <p
        v-if="!workflow.accept_new_blackboard_runs"
        role="status"
        class="completion-note is-warning"
      >
        当前已停止接收新的黑板任务，请选择按计划执行。已有任务仍可继续。
      </p>
    </div>
  </div>
</template>
