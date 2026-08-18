<script setup lang="ts">
import { computed, ref } from 'vue'
import {
  ChatDotRound,
  Connection,
  Delete,
  Moon,
  Plus,
  Search,
  Sunny,
} from '@element-plus/icons-vue'

import type { Conversation } from '../types'
import { formatCompactTime, STATUS_LABELS } from '../utils/run'

const props = defineProps<{
  conversations: Conversation[]
  activeId: string
  dark: boolean
  mobile?: boolean
}>()

const emit = defineEmits<{
  create: []
  select: [id: string]
  delete: [id: string]
  toggleTheme: []
}>()

const query = ref('')
const filteredConversations = computed(() => {
  const keyword = query.value.trim().toLocaleLowerCase()
  if (!keyword) return props.conversations
  return props.conversations.filter((item) =>
    item.title.toLocaleLowerCase().includes(keyword),
  )
})

function lastStatus(conversation: Conversation): string {
  const messages = [...conversation.messages].reverse()
  const status = messages.find((item) => item.status)?.status
  return status ? STATUS_LABELS[status] : conversation.messages.length > 0 ? '已记录' : '未开始'
}
</script>

<template>
  <aside class="sidebar" :class="{ 'mobile-sidebar': mobile }">
    <div class="brand">
      <span class="brand-mark"><Connection /></span>
      <div>
        <strong>Work Harness</strong>
        <span>智能协作台</span>
      </div>
    </div>

    <el-button class="new-chat" type="primary" :icon="Plus" @click="emit('create')">
      新建对话
    </el-button>

    <el-input
      v-model="query"
      class="history-search"
      clearable
      placeholder="搜索历史对话"
      :prefix-icon="Search"
    />

    <div class="history-section">
      <p class="section-label">最近对话</p>
      <el-empty
        v-if="filteredConversations.length === 0"
        :image-size="52"
        description="没有匹配的对话"
      />
      <button
        v-for="conversation in filteredConversations"
        :key="conversation.id"
        class="history-item"
        :class="{ active: conversation.id === activeId }"
        type="button"
        @click="emit('select', conversation.id)"
      >
        <ChatDotRound />
        <span>
          <strong>{{ conversation.title }}</strong>
          <small>
            {{ lastStatus(conversation) }} · {{ formatCompactTime(conversation.updatedAt) }}
          </small>
        </span>
        <el-popconfirm
          title="删除这条本地对话记录？"
          confirm-button-text="删除"
          cancel-button-text="保留"
          width="210"
          @confirm="emit('delete', conversation.id)"
        >
          <template #reference>
            <span
              class="history-delete"
              role="button"
              tabindex="0"
              aria-label="删除对话"
              @click.stop
              @keydown.enter.stop
            >
              <Delete />
            </span>
          </template>
        </el-popconfirm>
      </button>
    </div>

    <div class="sidebar-footer">
      <button type="button" @click="emit('toggleTheme')">
        <component :is="dark ? Sunny : Moon" />
        {{ dark ? '浅色模式' : '深色模式' }}
      </button>
      <p>历史记录仅保存在当前设备</p>
    </div>
  </aside>
</template>
