<script setup lang="ts">
import { computed, ref } from 'vue'
import type { Conversation } from '../types'
import { formatCompactTime, STATUS_LABELS } from '../utils/run'

const props = defineProps<{
  conversations: Conversation[]
  activeId: string
}>()
const emit = defineEmits<{
  create: []
  select: [id: string]
  delete: [id: string]
  pin: [id: string]
  rename: [id: string, title: string]
  history: []
}>()
const query = ref('')
const projectExpanded = ref(true)
const renaming = ref<Conversation | null>(null)
const renameTitle = ref('')
const renameOpen = computed({
  get: () => Boolean(renaming.value),
  set: (value) => {
    if (!value) renaming.value = null
  },
})
const deleting = ref<Conversation | null>(null)
const deleteOpen = computed({
  get: () => Boolean(deleting.value),
  set: (value) => {
    if (!value) deleting.value = null
  },
})
const filteredConversations = computed(() =>
  props.conversations.filter((item) =>
    item.title.toLocaleLowerCase().includes(query.value.trim().toLocaleLowerCase()),
  ),
)
const pinnedConversations = computed(() =>
  filteredConversations.value.filter((item) => item.pinned),
)
const regularConversations = computed(() =>
  filteredConversations.value.filter((item) => !item.pinned),
)
function lastStatus(conversation: Conversation): string {
  const status = [...conversation.messages].reverse().find((item) => item.status)?.status
  return status ? STATUS_LABELS[status] : '未开始'
}
function confirmDelete(): void {
  if (deleting.value) emit('delete', deleting.value.id)
  deleting.value = null
}

function startRename(conversation: Conversation): void {
  renameTitle.value = conversation.title
  renaming.value = conversation
}

function confirmRename(): void {
  const title = renameTitle.value.trim()
  if (!renaming.value || !title) return
  emit('rename', renaming.value.id, title)
  renaming.value = null
}

function handleRenameEnter(event: KeyboardEvent): void {
  if (event.isComposing || event.keyCode === 229) return
  event.preventDefault()
  confirmRename()
}

function selectRenameTitle(event: FocusEvent): void {
  const input = event.target as HTMLInputElement
  input.select()
}
</script>

<template>
  <aside class="sidebar-content">
    <nav class="primary-navigation" aria-label="工作空间导航">
      <UButton
        icon="i-lucide-square-pen"
        color="neutral"
        variant="ghost"
        block
        class="new-task-button"
        @click="emit('create')"
        >新任务<span class="shortcut-hint">＋</span></UButton
      >
      <UButton icon="i-lucide-clock" color="neutral" variant="ghost" block @click="emit('history')"
        >运行记录</UButton
      >
    </nav>
    <UInput
      v-model="query"
      icon="i-lucide-search"
      placeholder="搜索会话"
      aria-label="搜索会话"
      variant="soft"
      class="history-search"
    />
    <div class="history-section">
      <template v-if="pinnedConversations.length">
        <div class="sidebar-section-label">
          <span>置顶</span><span>{{ pinnedConversations.length }}</span>
        </div>
        <div
          v-for="conversation in pinnedConversations"
          :key="`pin-${conversation.id}`"
          class="history-item"
          :class="{ active: conversation.id === activeId }"
        >
          <button
            type="button"
            class="history-select"
            :title="`${conversation.title} · ${lastStatus(conversation)} · ${formatCompactTime(conversation.updatedAt)}`"
            :aria-current="conversation.id === activeId ? 'page' : undefined"
            @click="emit('select', conversation.id)"
          >
            <span class="history-text">
              <strong>{{ conversation.title }}</strong>
            </span>
          </button>
          <div class="history-item-actions">
            <UButton
              icon="i-lucide-pencil"
              color="neutral"
              variant="ghost"
              size="xs"
              class="history-rename"
              title="重命名"
              :aria-label="`重命名会话：${conversation.title}`"
              @click="startRename(conversation)"
            />
            <UButton
              icon="i-lucide-pin-off"
              color="neutral"
              variant="ghost"
              size="xs"
              class="history-pin"
              :aria-label="`取消置顶会话：${conversation.title}`"
              @click="emit('pin', conversation.id)"
            />
            <UButton
              icon="i-lucide-trash-2"
              color="neutral"
              variant="ghost"
              size="xs"
              class="history-delete"
              :aria-label="`删除会话：${conversation.title}`"
              @click="deleting = conversation"
            />
          </div>
        </div>
      </template>

      <div class="sidebar-section-label">
        <span>项目</span><span>{{ filteredConversations.length }}</span>
      </div>
      <div class="sidebar-project-group">
        <button
          type="button"
          class="sidebar-project-header"
          :aria-expanded="projectExpanded"
          @click="projectExpanded = !projectExpanded"
        >
          <UIcon
            :name="projectExpanded ? 'i-lucide-folder-open' : 'i-lucide-folder'"
            class="project-folder-icon"
          />
          <span>work-harness</span>
        </button>
        <p v-if="!filteredConversations.length" class="sidebar-empty">没有找到相关会话</p>
        <div v-else-if="projectExpanded" class="sidebar-project-items">
          <div
            v-for="conversation in regularConversations.length
              ? regularConversations
              : filteredConversations"
            :key="conversation.id"
            class="history-item"
            :class="{ active: conversation.id === activeId }"
          >
            <button
              type="button"
              class="history-select"
              :title="`${conversation.title} · ${lastStatus(conversation)} · ${formatCompactTime(conversation.updatedAt)}`"
              :aria-current="conversation.id === activeId ? 'page' : undefined"
              @click="emit('select', conversation.id)"
            >
              <span class="history-text">
                <strong>{{ conversation.title }}</strong>
                <small
                  >{{ lastStatus(conversation) }} ·
                  {{ formatCompactTime(conversation.updatedAt) }}</small
                >
              </span>
            </button>
            <div class="history-item-actions">
              <UButton
                icon="i-lucide-pencil"
                color="neutral"
                variant="ghost"
                size="xs"
                class="history-rename"
                title="重命名"
                :aria-label="`重命名会话：${conversation.title}`"
                @click="startRename(conversation)"
              />
              <UButton
                icon="i-lucide-pin"
                color="neutral"
                variant="ghost"
                size="xs"
                class="history-pin"
                :aria-label="`置顶会话：${conversation.title}`"
                @click="emit('pin', conversation.id)"
              />
              <UButton
                icon="i-lucide-trash-2"
                color="neutral"
                variant="ghost"
                size="xs"
                class="history-delete"
                :aria-label="`删除会话：${conversation.title}`"
                @click="deleting = conversation"
              />
            </div>
          </div>
        </div>
      </div>
    </div>
    <footer class="sidebar-footer">
      <small class="sidebar-storage-note">会话保存在此浏览器</small>
    </footer>
    <UModal v-model:open="renameOpen" title="重命名会话">
      <template #body>
        <UFormField label="会话名称">
          <UInput
            v-model="renameTitle"
            autofocus
            autocomplete="off"
            placeholder="输入会话名称"
            class="w-full"
            @focus="selectRenameTitle"
            @keydown.enter="handleRenameEnter"
          />
        </UFormField>
      </template>
      <template #footer>
        <UButton color="neutral" variant="outline" @click="renameOpen = false">取消</UButton>
        <UButton :disabled="!renameTitle.trim()" @click="confirmRename">保存名称</UButton>
      </template>
    </UModal>
    <UModal
      v-model:open="deleteOpen"
      title="删除本地会话？"
      description="将删除此浏览器保存的消息。服务端的运行记录仍可在运行记录中查看。"
    >
      <template #body
        ><p class="board-text">{{ deleting?.title }}</p></template
      >
      <template #footer
        ><UButton color="neutral" variant="outline" @click="deleteOpen = false">保留会话</UButton
        ><UButton color="error" @click="confirmDelete">确认删除</UButton></template
      >
    </UModal>
  </aside>
</template>
