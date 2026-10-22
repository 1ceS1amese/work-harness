<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { CircleCheck, Refresh } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'

import { harnessApi } from '../services/api'
import type { ApplicationSettingsDescriptor, EditableRuntimeSettings } from '../types'

const settings = ref<ApplicationSettingsDescriptor | null>(null)
const loading = ref(false)
const saving = ref(false)
const form = reactive<EditableRuntimeSettings>(emptySettings())

const reviewStrategies = [
  { value: 'adaptive', label: '按任务风险自动决定' },
  { value: 'comprehensive', label: '每份结果做一次综合检查' },
  { value: 'three_dimensional', label: '始终进行事实、逻辑和要求三类检查' },
] as const
const riskOptions = [
  { value: 'low', label: '低风险' },
  { value: 'medium', label: '中风险' },
  { value: 'high', label: '高风险' },
  { value: 'critical', label: '关键风险' },
]
const taskOptions = [
  { value: 'general', label: '日常任务' },
  { value: 'planning', label: '安排计划' },
  { value: 'decision', label: '帮我做选择' },
  { value: 'research', label: '查资料' },
  { value: 'code', label: '写代码' },
]

function emptySettings(): EditableRuntimeSettings {
  return {
    run_max_concurrency: 4,
    run_queue_capacity: 8,
    run_queue_retry_after_seconds: 5,
    worker_max_concurrency: 4,
    reviewer_max_concurrency: 3,
    review_strategy: 'adaptive',
    deep_review_risk_levels: ['high', 'critical'],
    deep_review_task_types: ['research', 'decision'],
    max_planned_subtasks: 4,
    max_repair_rounds: 1,
    node_timeout_seconds: 60,
    run_timeout_seconds: 300,
    max_artifact_chars: 20000,
    max_tool_result_chars: 8000,
    model_retry_attempts: 2,
  }
}

function applySettings(value: ApplicationSettingsDescriptor): void {
  settings.value = value
  Object.assign(form, value.runtime)
}

async function loadSettings(): Promise<void> {
  loading.value = true
  try {
    applySettings(await harnessApi.settings())
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '无法读取运行配置')
  } finally {
    loading.value = false
  }
}

async function saveSettings(): Promise<void> {
  saving.value = true
  try {
    applySettings(await harnessApi.saveRuntimeSettings({ ...form }))
    ElMessage.success('运行配置已保存，重启后生效')
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '运行配置保存失败')
  } finally {
    saving.value = false
  }
}

onMounted(() => void loadSettings())
</script>

<template>
  <section class="settings-panel runtime-settings-panel">
    <header class="settings-content-heading">
      <div>
        <p class="eyebrow">工作流策略</p>
        <h2>运行配置</h2>
        <p>只开放任务质量、并发、超时和结果长度参数；数据库路径与安全边界仍由部署配置管理。</p>
      </div>
      <el-button :icon="Refresh" :loading="loading" @click="loadSettings">重新读取</el-button>
    </header>

    <el-alert
      v-if="settings?.restart_required"
      title="运行配置正在等待重启"
      description="重启后创建的新任务会使用这些参数，正在运行的任务不会中途改变。"
      type="warning"
      show-icon
      :closable="false"
    />

    <el-skeleton v-if="loading && !settings" :rows="10" animated />
    <el-form v-else label-position="top" class="runtime-settings-form" @submit.prevent>
      <section class="config-card">
        <div class="config-card-heading">
          <div>
            <h3>并发与任务规模</h3>
            <p>控制同一时间处理多少任务，以及一个任务最多拆成多少份工作。</p>
          </div>
        </div>
        <div class="config-grid">
          <el-form-item label="同时运行的任务数">
            <el-input-number v-model="form.run_max_concurrency" :min="1" :max="32" />
          </el-form-item>
          <el-form-item label="最多等待的任务数">
            <el-input-number v-model="form.run_queue_capacity" :min="0" :max="10000" />
          </el-form-item>
          <el-form-item label="队列已满后建议重试（秒）">
            <el-input-number v-model="form.run_queue_retry_after_seconds" :min="1" :max="300" />
          </el-form-item>
          <el-form-item label="同时工作的智能成员数">
            <el-input-number v-model="form.worker_max_concurrency" :min="1" :max="32" />
          </el-form-item>
          <el-form-item label="同时进行的检查数">
            <el-input-number v-model="form.reviewer_max_concurrency" :min="1" :max="32" />
          </el-form-item>
          <el-form-item label="每个任务最多拆分数">
            <el-input-number v-model="form.max_planned_subtasks" :min="1" :max="20" />
          </el-form-item>
        </div>
      </section>

      <section class="config-card">
        <div class="config-card-heading">
          <div>
            <h3>检查与返工</h3>
            <p>普通任务可以保持轻量，高风险任务再自动提高检查强度。</p>
          </div>
        </div>
        <el-form-item label="检查方式">
          <el-select v-model="form.review_strategy">
            <el-option
              v-for="option in reviewStrategies"
              :key="option.value"
              :label="option.label"
              :value="option.value"
            />
          </el-select>
        </el-form-item>
        <div class="config-grid">
          <el-form-item label="哪些风险等级进行深度检查">
            <el-checkbox-group v-model="form.deep_review_risk_levels">
              <el-checkbox
                v-for="option in riskOptions"
                :key="option.value"
                :label="option.value"
                :value="option.value"
              >
                {{ option.label }}
              </el-checkbox>
            </el-checkbox-group>
          </el-form-item>
          <el-form-item label="哪些任务类型进行深度检查">
            <el-checkbox-group v-model="form.deep_review_task_types">
              <el-checkbox
                v-for="option in taskOptions"
                :key="option.value"
                :label="option.value"
                :value="option.value"
              >
                {{ option.label }}
              </el-checkbox>
            </el-checkbox-group>
          </el-form-item>
          <el-form-item label="最多返工轮数">
            <el-input-number v-model="form.max_repair_rounds" :min="0" :max="5" />
          </el-form-item>
          <el-form-item label="模型失败重试次数">
            <el-input-number v-model="form.model_retry_attempts" :min="0" :max="5" />
          </el-form-item>
        </div>
      </section>

      <section class="config-card">
        <div class="config-card-heading">
          <div>
            <h3>时间与输出长度</h3>
            <p>设置执行超时与结果长度。Token 用量仅作记录，不设置累计额度。</p>
          </div>
        </div>
        <div class="config-grid">
          <el-form-item label="单个步骤超时（秒）">
            <el-input-number v-model="form.node_timeout_seconds" :min="5" :max="600" />
          </el-form-item>
          <el-form-item label="整个任务超时（秒）">
            <el-input-number v-model="form.run_timeout_seconds" :min="10" :max="3600" />
          </el-form-item>
          <el-form-item label="单份工作结果最大字符数">
            <el-input-number v-model="form.max_artifact_chars" :min="100" :max="200000" :step="1000" />
          </el-form-item>
          <el-form-item label="单次工具结果最大字符数">
            <el-input-number v-model="form.max_tool_result_chars" :min="100" :max="100000" :step="500" />
          </el-form-item>
        </div>
      </section>

      <div class="settings-save-bar">
        <span>保存后需要重启 Work Harness，不会影响正在运行的任务。</span>
        <el-button type="primary" :icon="CircleCheck" :loading="saving" @click="saveSettings">
          保存运行配置
        </el-button>
      </div>
    </el-form>
  </section>
</template>
