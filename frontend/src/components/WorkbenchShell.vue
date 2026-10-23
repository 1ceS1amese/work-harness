<script setup lang="ts">
import { computed } from 'vue'
const mobileMenuOpen = defineModel<boolean>('mobileMenuOpen', { default: false })
const props = withDefaults(
  defineProps<{
    section: 'workspace' | 'settings'
    title: string
    connectionLabel: string
    connectionState?: 'checking' | 'online' | 'setup' | 'offline' | 'auth'
    dark?: boolean
  }>(),
  { connectionState: 'checking', dark: false },
)
const emit = defineEmits<{
  history: []
  toggleTheme: []
  refresh: []
  authenticate: []
  workspace: []
  settings: []
  create: []
}>()
const menuItems = computed(() => [
  ...(props.section === 'workspace'
    ? [{ label: '刷新运行状态', icon: 'i-lucide-refresh-cw', onSelect: () => emit('refresh') }]
    : [{ label: '运行记录', icon: 'i-lucide-history', onSelect: () => emit('history') }]),
  {
    label: props.dark ? '切换到浅色模式' : '切换到深色模式',
    icon: props.dark ? 'i-lucide-sun' : 'i-lucide-moon',
    onSelect: () => emit('toggleTheme'),
  },
  { label: '工作台访问验证', icon: 'i-lucide-key-round', onSelect: () => emit('authenticate') },
])
</script>

<template>
  <!-- One dashboard shell keeps navigation dimensions and persistence identical on every page. -->
  <UDashboardGroup unit="rem" storage="local" class="workbench-shell" :class="`${section}-page`">
    <a class="skip-link" href="#workbench-content">跳到主要内容</a>
    <nav class="codex-rail" aria-label="全局快捷栏">
      <div class="codex-rail-top">
        <button
          type="button"
          class="codex-rail-btn"
          :class="{ active: section === 'workspace' }"
          aria-label="切换到工作空间"
          title="工作空间"
          @click="emit('workspace')"
        >
          <UIcon name="i-lucide-house" />
        </button>
        <button
          type="button"
          class="codex-rail-btn"
          aria-label="快速新建任务"
          title="新任务"
          @click="emit('create')"
        >
          <UIcon name="i-lucide-layers" />
        </button>
        <button
          type="button"
          class="codex-rail-btn"
          aria-label="打开运行历史"
          title="运行记录"
          @click="emit('history')"
        >
          <UIcon name="i-lucide-clock" />
        </button>
      </div>
      <div class="codex-rail-bottom">
        <button
          type="button"
          class="codex-rail-btn"
          aria-label="切换外观主题"
          :title="dark ? '切换到浅色模式' : '切换到深色模式'"
          @click="emit('toggleTheme')"
        >
          <UIcon :name="dark ? 'i-lucide-sun' : 'i-lucide-moon'" />
        </button>
        <button
          type="button"
          class="codex-rail-avatar"
          :class="{ active: section === 'settings' }"
          :aria-current="section === 'settings' ? 'page' : undefined"
          aria-label="打开工作空间设置"
          title="设置"
          @click="emit('settings')"
        >
          <span>WH</span>
          <i :class="`state-${connectionState}`" />
        </button>
      </div>
    </nav>
    <UDashboardSidebar
      id="work-harness-navigation"
      v-model:open="mobileMenuOpen"
      :default-size="14"
      :min-size="12"
      :max-size="20"
      resizable
      :ui="{ body: 'p-0 gap-0 overflow-hidden', header: 'lg:hidden', footer: 'lg:hidden' }"
      class="workspace-sidebar"
    >
      <div class="workspace-brand">
        <div class="workspace-brand-title">
          <strong>Work Harness</strong>
          <UIcon name="i-lucide-chevron-down" class="brand-chevron" />
        </div>
        <span class="workspace-brand-label">本地</span>
      </div>
      <slot name="sidebar" />
      <template #footer>
        <button
          type="button"
          class="codex-rail-avatar"
          :class="{ active: section === 'settings' }"
          :aria-current="section === 'settings' ? 'page' : undefined"
          aria-label="打开工作空间设置"
          title="设置"
          @click="emit('settings')"
        >
          <span>WH</span>
          <i :class="`state-${connectionState}`" />
        </button>
      </template>
    </UDashboardSidebar>
    <UDashboardPanel
      id="work-harness-main"
      :ui="{ body: 'p-0 sm:p-0 gap-0' }"
      class="workspace-panel"
    >
      <template #header>
        <UDashboardNavbar
          :toggle="{ 'aria-label': section === 'settings' ? '打开设置导航' : '打开会话列表' }"
          :ui="{ root: 'workspace-navbar', left: 'min-w-0 flex-1', right: 'gap-1 shrink-0' }"
        >
          <template #title>
            <div class="workspace-breadcrumb" :title="title">
              <UIcon
                :name="section === 'settings' ? 'i-lucide-sliders-horizontal' : 'i-lucide-folder'"
                class="breadcrumb-folder-icon"
              />
              <span class="breadcrumb-section">{{
                section === 'settings' ? '设置' : '工作空间'
              }}</span>
              <UIcon name="i-lucide-chevron-right" class="breadcrumb-separator" />
              <span class="active-title">{{ title }}</span>
            </div>
          </template>
          <template #right>
            <span
              class="connection-status"
              :title="connectionLabel"
              :aria-label="connectionLabel"
              role="status"
            >
              <i :class="`state-${connectionState}`" /><span>{{ connectionLabel }}</span>
            </span>
            <slot name="header-actions" />
            <UDropdownMenu :items="menuItems" :content="{ align: 'end' }">
              <UButton
                icon="i-lucide-ellipsis"
                color="neutral"
                variant="ghost"
                aria-label="更多操作"
              />
            </UDropdownMenu>
          </template>
        </UDashboardNavbar>
      </template>
      <template #body><slot /></template>
    </UDashboardPanel>
    <slot name="overlay" />
  </UDashboardGroup>
</template>
