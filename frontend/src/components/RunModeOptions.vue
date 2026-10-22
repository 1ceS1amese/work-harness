<script setup lang="ts">
import { computed } from 'vue'
import type { WorkflowCapabilities } from '../types'
import { effectiveEngine, type RunMode } from '../utils/admission'

const mode = defineModel<RunMode>({ required: true })
const templateId = defineModel<string>('templateId', { required: true })
const props = defineProps<{ workflow: WorkflowCapabilities; disabled: boolean }>()
const blackboard = computed(() => effectiveEngine(mode.value, props.workflow) === 'blackboard')
const selected = computed(() =>
  props.workflow.blackboard_templates.find((item) => item.id === templateId.value),
)
</script>

<template>
  <div class="run-mode-options">
    <label>
      协作方式
      <select v-model="mode" :disabled="disabled" aria-label="协作方式">
        <option value="default">
          跟随项目设置（{{ workflow.default_engine === 'blackboard' ? '黑板协作' : '按计划执行' }}）
        </option>
        <option value="legacy">按计划执行</option>
        <option value="blackboard" :disabled="!workflow.accept_new_blackboard_runs">黑板协作</option>
      </select>
    </label>
    <label v-if="blackboard && workflow.accept_new_blackboard_runs">
      完成条件
      <select v-model="templateId" :disabled="disabled" aria-label="黑板任务完成条件">
        <option value="" disabled>请选择完成条件</option>
        <option v-for="item in workflow.blackboard_templates" :key="item.id" :value="item.id">
          {{ item.title }}
        </option>
      </select>
    </label>
    <p v-if="blackboard && !workflow.accept_new_blackboard_runs" role="status">
      当前已停止接收新的黑板任务，请选择按计划执行。已有任务仍可继续。
    </p>
    <p v-else-if="blackboard" role="note">
      {{ selected?.description ?? '请明确选择完成条件；普通任务目标不会自动视为已完成。' }}
    </p>
  </div>
</template>

<style scoped>
.run-mode-options { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 16px; margin-bottom: 10px; }
label { display: flex; align-items: center; gap: 8px; font-size: 13px; }
select { max-width: 100%; padding: 5px 8px; border: 1px solid var(--el-border-color); border-radius: 5px; background: var(--el-bg-color); color: inherit; }
p { width: 100%; margin: 0; color: var(--el-text-color-secondary); font-size: 12px; }
</style>
