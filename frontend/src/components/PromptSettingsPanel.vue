<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { CircleCheck, Refresh, RefreshLeft } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'

import { harnessApi } from '../services/api'
import type { ApplicationSettingsDescriptor, PromptSettingDescriptor } from '../types'

const settings = ref<ApplicationSettingsDescriptor | null>(null)
const selectedName = ref('')
const draft = ref('')
const loading = ref(false)
const saving = ref(false)
const resetting = ref(false)

const selected = computed<PromptSettingDescriptor | null>(() =>
  settings.value?.prompts.find((prompt) => prompt.name === selectedName.value) ?? null,
)
const draftLength = computed(() => draft.value.trim().length)

function selectPrompt(prompt: PromptSettingDescriptor): void {
  selectedName.value = prompt.name
  draft.value = prompt.body
}

function applySettings(value: ApplicationSettingsDescriptor): void {
  settings.value = value
  const current = value.prompts.find((prompt) => prompt.name === selectedName.value)
    ?? value.prompts[0]
  if (current) selectPrompt(current)
}

async function loadSettings(): Promise<void> {
  loading.value = true
  try {
    applySettings(await harnessApi.settings())
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '无法读取 Agent 提示词')
  } finally {
    loading.value = false
  }
}

async function savePrompt(): Promise<void> {
  if (!selected.value) return
  const body = draft.value.trim()
  if (!body) {
    ElMessage.warning('提示词不能为空')
    return
  }
  saving.value = true
  try {
    applySettings(await harnessApi.savePrompt(selected.value.name, body))
    ElMessage.success('Agent 提示词已保存，重启后生效')
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '提示词保存失败')
  } finally {
    saving.value = false
  }
}

async function resetPrompt(): Promise<void> {
  if (!selected.value?.customized) return
  resetting.value = true
  try {
    applySettings(await harnessApi.resetPrompt(selected.value.name))
    ElMessage.success('已恢复内置提示词，重启后生效')
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '无法恢复内置提示词')
  } finally {
    resetting.value = false
  }
}

onMounted(() => void loadSettings())
</script>

<template>
  <section class="settings-panel prompt-settings-panel">
    <header class="settings-content-heading">
      <div>
        <p class="eyebrow">Agent 行为</p>
        <h2>Agent 提示词</h2>
        <p>为每个工作阶段调整职责和输出原则。智能成员共用一份执行提示词。</p>
      </div>
      <el-button :icon="Refresh" :loading="loading" @click="loadSettings">重新读取</el-button>
    </header>

    <el-alert
      v-if="settings?.restart_required"
      title="提示词修改正在等待重启"
      description="新任务会在 Work Harness 重启后使用这里保存的提示词。"
      type="warning"
      show-icon
      :closable="false"
    />

    <el-skeleton v-if="loading && !settings" :rows="8" animated />
    <div v-else-if="settings" class="prompt-workspace">
      <nav class="prompt-role-list" aria-label="Agent 提示词列表">
        <button
          v-for="prompt in settings.prompts"
          :key="prompt.name"
          type="button"
          :class="{ active: prompt.name === selectedName }"
          @click="selectPrompt(prompt)"
        >
          <span>
            <strong>{{ prompt.display_name }}</strong>
            <small>{{ prompt.description }}</small>
          </span>
          <el-tag v-if="prompt.customized" size="small" effect="plain">已自定义</el-tag>
        </button>
      </nav>

      <article v-if="selected" class="prompt-editor-card">
        <div class="prompt-editor-heading">
          <div>
            <strong>{{ selected.display_name }}</strong>
            <small>
              {{ selected.customized ? '当前使用页面保存的版本' : '当前使用内置版本' }}
              · 版本 {{ selected.version }}
            </small>
          </div>
          <el-tag :type="selected.loaded ? 'success' : 'warning'" effect="plain">
            {{ selected.loaded ? '运行中已装载' : '等待重启' }}
          </el-tag>
        </div>
        <el-input
          v-model="draft"
          type="textarea"
          :rows="18"
          resize="vertical"
          maxlength="50000"
          placeholder="写清楚这个 Agent 应该做什么、不能做什么，以及结果应满足哪些要求。"
        />
        <div class="prompt-editor-footer">
          <small>{{ draftLength.toLocaleString() }} / 50,000 字符</small>
          <span>
            <el-popconfirm
              v-if="selected.customized"
              title="恢复这个 Agent 的内置提示词？"
              confirm-button-text="恢复"
              cancel-button-text="取消"
              @confirm="resetPrompt"
            >
              <template #reference>
                <el-button :icon="RefreshLeft" :loading="resetting">恢复内置版本</el-button>
              </template>
            </el-popconfirm>
            <el-button
              type="primary"
              :icon="CircleCheck"
              :loading="saving"
              @click="savePrompt"
            >
              保存提示词
            </el-button>
          </span>
        </div>
      </article>
    </div>
  </section>
</template>
