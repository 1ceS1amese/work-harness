<script setup lang="ts">
import { ref, watch } from 'vue'
import {
  ArrowLeft,
  ChatLineSquare,
  Cpu,
  Lock,
  Moon,
  Operation,
  Sunny,
} from '@element-plus/icons-vue'

import type { AgentDescriptor } from '../types'
import PromptSettingsPanel from './PromptSettingsPanel.vue'
import ProviderSettingsPanel from './ProviderSettingsPanel.vue'
import RuntimeSettingsPanel from './RuntimeSettingsPanel.vue'

type SettingsSection = 'models' | 'prompts' | 'runtime'

const props = withDefaults(defineProps<{
  logicalModels: string[]
  agents: AgentDescriptor[]
  setupMode?: boolean
  initialSection?: SettingsSection
  dark?: boolean
  connectionLabel: string
}>(), {
  setupMode: false,
  initialSection: 'models',
  dark: false,
})

const emit = defineEmits<{
  back: []
  changed: []
  toggleTheme: []
  authenticate: []
  sectionChange: [section: SettingsSection]
}>()

const activeSection = ref<SettingsSection>(props.initialSection)
const sections: Array<{
  id: SettingsSection
  label: string
  description: string
  icon: typeof Cpu
}> = [
  { id: 'models', label: '模型设置', description: '服务、密钥与成员模型', icon: Cpu },
  { id: 'prompts', label: 'Agent 提示词', description: '每个工作阶段的行为要求', icon: ChatLineSquare },
  { id: 'runtime', label: '运行配置', description: '并发、检查与超时', icon: Operation },
]

function chooseSection(section: SettingsSection): void {
  activeSection.value = section
  emit('sectionChange', section)
}

watch(
  () => props.initialSection,
  (value) => { activeSection.value = value },
)
</script>

<template>
  <div class="settings-page">
    <header class="settings-topbar">
      <div class="settings-topbar-main">
        <el-button :icon="ArrowLeft" text @click="emit('back')">返回协作群聊</el-button>
        <span class="settings-topbar-divider" />
        <div>
          <strong>Work Harness 设置</strong>
          <small>{{ connectionLabel }}</small>
        </div>
      </div>
      <div class="settings-topbar-actions">
        <el-button
          circle
          :icon="dark ? Sunny : Moon"
          :aria-label="dark ? '切换到浅色模式' : '切换到深色模式'"
          @click="emit('toggleTheme')"
        />
        <el-button circle :icon="Lock" aria-label="部署 API 身份验证" @click="emit('authenticate')" />
      </div>
    </header>

    <div class="settings-layout">
      <aside class="settings-navigation">
        <div class="settings-navigation-heading">
          <p class="eyebrow">系统设置</p>
          <h1>配置工作台</h1>
          <p>模型、Agent 行为和运行策略相互独立，可以按需组合。</p>
        </div>
        <nav aria-label="设置分类">
          <button
            v-for="section in sections"
            :key="section.id"
            type="button"
            :class="{ active: activeSection === section.id }"
            @click="chooseSection(section.id)"
          >
            <component :is="section.icon" />
            <span>
              <strong>{{ section.label }}</strong>
              <small>{{ section.description }}</small>
            </span>
          </button>
        </nav>
        <div class="settings-navigation-note">
          <strong>保存与生效</strong>
          <p>设置保存在数据目录，不修改发布包。模型、提示词和运行参数保存后都需要重启服务。</p>
        </div>
      </aside>

      <main class="settings-main">
        <ProviderSettingsPanel
          v-if="activeSection === 'models'"
          :logical-models="logicalModels"
          :agents="agents"
          :setup-mode="setupMode"
          @changed="emit('changed')"
        />
        <PromptSettingsPanel v-else-if="activeSection === 'prompts'" />
        <RuntimeSettingsPanel v-else />
      </main>
    </div>
  </div>
</template>
