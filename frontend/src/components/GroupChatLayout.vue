<script setup lang="ts">
import { Menu, UserFilled } from '@element-plus/icons-vue'

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
          <div class="chat-title-row">
            <h1>{{ title }}</h1>
            <small>({{ members.length + 1 }})</small>
          </div>
          <div class="chat-connection">
            <span class="live-dot" :class="`state-${connectionState}`" />
            {{ connectionLabel }}
            <span>·</span>
            <span>{{ members.length }} 位 AI 小伙伴在群里</span>
          </div>
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
            <small>群成员</small>
          </div>
        </div>
        <div class="header-actions">
          <slot name="header-actions" />
        </div>
      </header>

      <slot name="conversation" />
      <slot name="composer" />
    </main>

    <aside class="group-info-panel">
      <section class="group-info-section group-notice">
        <h2>群公告</h2>
        <p>把事情直接发到群里就行。任务管家会安排分工，大家做完后再互相检查一遍。</p>
      </section>
      <section class="group-info-section">
        <div class="group-info-heading">
          <h2>群成员</h2>
          <span>{{ members.length + 1 }} 人</span>
        </div>
        <div class="group-member-list">
          <div class="group-member owner-member">
            <i><UserFilled /></i>
            <span><strong>你</strong><small>群主</small></span>
          </div>
          <div v-for="member in members" :key="member.id" class="group-member">
            <i :class="`avatar-${member.id.replace('worker-', '')}`">
              {{ member.display_name.slice(0, 1) }}
            </i>
            <span>
              <strong>{{ member.display_name }}</strong>
              <small>随时可以接活</small>
            </span>
          </div>
        </div>
      </section>
      <p class="group-info-tip">小提示：输入 @成员，可以直接把这次任务交给他。</p>
    </aside>

    <slot name="overlay" />
  </div>
</template>
