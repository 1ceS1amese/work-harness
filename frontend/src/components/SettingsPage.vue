<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import WorkbenchShell from './WorkbenchShell.vue'

import type { AgentDescriptor } from '../types'
import PromptSettingsPanel from './PromptSettingsPanel.vue'
import ProviderSettingsPanel from './ProviderSettingsPanel.vue'
import RuntimeSettingsPanel from './RuntimeSettingsPanel.vue'

type SettingsSection = 'models' | 'prompts' | 'runtime'

const props = withDefaults(
  defineProps<{
    logicalModels: string[]
    agents: AgentDescriptor[]
    setupMode?: boolean
    initialSection?: SettingsSection
    dark?: boolean
    connectionLabel: string
    connectionState?: 'checking' | 'online' | 'setup' | 'offline' | 'auth'
  }>(),
  {
    setupMode: false,
    initialSection: 'models',
    dark: false,
  },
)

const emit = defineEmits<{
  back: []
  history: []
  changed: []
  toggleTheme: []
  authenticate: []
  sectionChange: [section: SettingsSection]
}>()

const mobileMenuOpen = ref(false)
const activeSection = ref<SettingsSection>(props.initialSection)
const sections: Array<{
  id: SettingsSection
  label: string
  description: string
  icon: string
}> = [
  { id: 'models', label: '模型连接', description: '服务地址、密钥与模型', icon: 'i-lucide-cpu' },
  {
    id: 'prompts',
    label: 'Agent 提示词',
    description: '每个工作阶段的行为要求',
    icon: 'i-lucide-message-square-text',
  },
  {
    id: 'runtime',
    label: '运行配置',
    description: '并发、检查与超时',
    icon: 'i-lucide-sliders-horizontal',
  },
]

const activeTitle = computed(
  () => sections.find((section) => section.id === activeSection.value)!.label,
)

function chooseSection(section: SettingsSection): void {
  mobileMenuOpen.value = false
  activeSection.value = section
  emit('sectionChange', section)
}

watch(
  () => props.initialSection,
  (value) => {
    activeSection.value = value
  },
)
</script>

<template>
  <WorkbenchShell
    v-model:mobile-menu-open="mobileMenuOpen"
    section="settings"
    :title="activeTitle"
    :connection-label="connectionLabel"
    :connection-state="connectionState"
    :dark="dark"
    @history="emit('history')"
    @toggle-theme="emit('toggleTheme')"
    @authenticate="emit('authenticate')"
    @settings="chooseSection('models')"
    @workspace="emit('back')"
    @create="emit('back')"
  >
    <template #sidebar>
      <aside class="sidebar-content settings-sidebar-content">
        <UButton
          icon="i-lucide-arrow-left"
          color="neutral"
          variant="ghost"
          class="settings-back"
          @click="emit('back')"
          >返回工作空间</UButton
        >
        <p class="sidebar-section-label">工作空间设置</p>
        <nav aria-label="设置分类" class="settings-navigation">
          <button
            v-for="section in sections"
            :key="section.id"
            type="button"
            :class="{ active: activeSection === section.id }"
            :aria-current="activeSection === section.id ? 'page' : undefined"
            @click="chooseSection(section.id)"
          >
            <UIcon :name="section.icon" /><span
              ><strong>{{ section.label }}</strong
              ><small>{{ section.description }}</small></span
            >
          </button>
        </nav>
        <div class="settings-navigation-note">
          <UIcon name="i-lucide-info" />
          <p>设置在重启服务后生效。</p>
        </div>
      </aside>
    </template>
    <template #header-actions>
      <UButton
        icon="i-lucide-arrow-left"
        color="neutral"
        variant="ghost"
        aria-label="返回工作空间"
        class="lg:hidden"
        @click="emit('back')"
      />
    </template>
    <main id="workbench-content" tabindex="-1" class="settings-main">
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
  </WorkbenchShell>
</template>
