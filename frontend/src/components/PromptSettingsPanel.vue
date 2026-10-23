<script setup lang="ts">
import ConfirmAction from './ConfirmAction.vue'
import { computed, onMounted, ref } from 'vue'
import { useNotice } from '../composables/useNotice'

import { harnessApi } from '../services/api'
import type { ApplicationSettingsDescriptor, PromptSettingDescriptor } from '../types'

const notice = useNotice()

const settings = ref<ApplicationSettingsDescriptor | null>(null)
const selectedName = ref('')
const draft = ref('')
const loading = ref(false)
const loadFailed = ref(false)
const saving = ref(false)
const resetting = ref(false)

const selected = computed<PromptSettingDescriptor | null>(
  () => settings.value?.prompts.find((prompt) => prompt.name === selectedName.value) ?? null,
)
const draftLength = computed(() => draft.value.trim().length)

function selectPrompt(prompt: PromptSettingDescriptor): void {
  selectedName.value = prompt.name
  draft.value = prompt.body
}

function applySettings(value: ApplicationSettingsDescriptor): void {
  settings.value = value
  const current =
    value.prompts.find((prompt) => prompt.name === selectedName.value) ?? value.prompts[0]
  if (current) selectPrompt(current)
}

async function loadSettings(): Promise<void> {
  loading.value = true
  loadFailed.value = false
  try {
    applySettings(await harnessApi.settings())
  } catch (error) {
    loadFailed.value = true
    notice.error(error instanceof Error ? error.message : '无法读取 Agent 提示词')
  } finally {
    loading.value = false
  }
}

async function savePrompt(): Promise<void> {
  if (!selected.value) return
  const body = draft.value.trim()
  if (!body) {
    notice.warning('提示词不能为空')
    return
  }
  saving.value = true
  try {
    applySettings(await harnessApi.savePrompt(selected.value.name, body))
    notice.success('Agent 提示词已保存，重启后生效')
  } catch (error) {
    notice.error(error instanceof Error ? error.message : '提示词保存失败')
  } finally {
    saving.value = false
  }
}

async function resetPrompt(): Promise<void> {
  if (!selected.value?.customized) return
  resetting.value = true
  try {
    applySettings(await harnessApi.resetPrompt(selected.value.name))
    notice.success('已恢复内置提示词，重启后生效')
  } catch (error) {
    notice.error(error instanceof Error ? error.message : '无法恢复内置提示词')
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
        <h2>Agent 提示词</h2>
        <p>为每个工作阶段调整职责和输出原则。执行 Agent 共用一份提示词。</p>
      </div>
      <UButton
        v-if="loadFailed"
        icon="i-lucide-refresh-cw"
        :loading="loading"
        @click="loadSettings"
        color="neutral"
        variant="outline"
        >重新读取</UButton
      >
    </header>

    <UAlert
      v-if="settings?.restart_required"
      title="提示词修改正在等待重启"
      description="新任务会在 Work Harness 重启后使用这里保存的提示词。"
      color="warning"
      variant="subtle"
    />

    <USkeleton v-if="loading && !settings" />
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
          <UBadge v-if="prompt.customized" size="sm" variant="subtle">已自定义</UBadge>
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
          <UBadge :color="selected.loaded ? 'success' : 'warning'" variant="subtle">
            {{ selected.loaded ? '运行中已装载' : '等待重启' }}
          </UBadge>
        </div>
        <UTextarea
          v-model="draft"
          aria-label="Agent 提示词"
          :rows="18"
          resize="vertical"
          maxlength="50000"
          placeholder="写清楚这个 Agent 应该做什么、不能做什么，以及结果应满足哪些要求。"
          class="w-full"
        />
        <div class="prompt-editor-footer">
          <small>{{ draftLength.toLocaleString() }} / 50,000 字符</small>
          <span>
            <ConfirmAction
              v-if="selected.customized"
              title="恢复这个 Agent 的内置提示词？"
              confirm-button-text="恢复"
              cancel-button-text="取消"
              @confirm="resetPrompt"
            >
              <template #reference>
                <UButton
                  icon="i-lucide-rotate-ccw"
                  :loading="resetting"
                  color="neutral"
                  variant="outline"
                  >恢复内置版本</UButton
                >
              </template>
            </ConfirmAction>
            <UButton
              color="neutral"
              icon="i-lucide-circle-check"
              :loading="saving"
              @click="savePrompt"
            >
              保存提示词
            </UButton>
          </span>
        </div>
      </article>
    </div>
  </section>
</template>
