<script setup lang="ts">
import { Menu } from '@element-plus/icons-vue'

import type { AgentDescriptor } from '../types'

const mobileMenuOpen = defineModel<boolean>('mobileMenuOpen', { required: true })

defineProps<{
  title: string
  connectionLabel: string
  connectionState: 'checking' | 'online' | 'offline' | 'auth'
  members: AgentDescriptor[]
}>()
</script>

<template>
  <div class="app-shell group-chat-layout">
    <slot name="sidebar" />

    <el-drawer
      v-model="mobileMenuOpen"
      direction="ltr"
      size="286px"
      :with-header="false"
      class="mobile-nav-drawer"
    >
      <slot name="mobile-sidebar" />
    </el-drawer>

    <main class="chat-panel">
      <header class="chat-header">
        <el-button
          class="mobile-menu-button"
          circle
          :icon="Menu"
          aria-label="打开群聊列表"
          @click="mobileMenuOpen = true"
        />
        <div class="chat-title">
          <div class="eyebrow">
            <span class="live-dot" :class="`state-${connectionState}`" />
            {{ connectionLabel }}
          </div>
          <h1>{{ title }}</h1>
          <div class="group-presence" aria-label="群聊成员">
            <span class="presence-avatars">
              <i
                v-for="member in members.slice(0, 6)"
                :key="member.id"
                class="agent-avatar"
                :class="`avatar-${member.id.replace('worker-', '')}`"
                :title="member.display_name"
              >
                {{ member.display_name.slice(0, 1) }}
              </i>
            </span>
            <small>{{ members.length }} 位成员 · 正在协作</small>
          </div>
        </div>
        <div class="header-actions">
          <slot name="header-actions" />
        </div>
      </header>

      <slot name="conversation" />
      <slot name="composer" />
    </main>

    <slot name="overlay" />
  </div>
</template>
