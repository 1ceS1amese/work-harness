<script setup lang="ts">
import WorkbenchShell from './WorkbenchShell.vue'

const mobileMenuOpen = defineModel<boolean>('mobileMenuOpen', { required: true })
defineProps<{
  title: string
  connectionLabel: string
  connectionState: 'checking' | 'online' | 'setup' | 'offline' | 'auth'
  empty?: boolean
  dark?: boolean
}>()
const emit = defineEmits<{
  history: []
  toggleTheme: []
  refresh: []
  authenticate: []
  settings: []
  create: []
}>()
</script>

<template>
  <WorkbenchShell
    v-model:mobile-menu-open="mobileMenuOpen"
    section="workspace"
    :title="title"
    :connection-label="connectionLabel"
    :connection-state="connectionState"
    :dark="dark"
    @history="emit('history')"
    @toggle-theme="emit('toggleTheme')"
    @refresh="emit('refresh')"
    @authenticate="emit('authenticate')"
    @settings="emit('settings')"
    @create="emit('create')"
  >
    <template #sidebar><slot name="sidebar" /></template>
    <main id="workbench-content" tabindex="-1" class="chat-stage" :class="{ 'is-empty': empty }">
      <slot name="conversation" />
      <div class="input-stage"><slot name="composer" /></div>
    </main>
    <template #overlay><slot name="overlay" /></template>
  </WorkbenchShell>
</template>
