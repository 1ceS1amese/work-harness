<script setup lang="ts">
import { onBeforeUnmount, ref, shallowRef, watch } from 'vue'
import { harnessApi } from '../services/api'
import type { RunList } from '../types'
import { displayTime } from '../utils/blackboard'
import { STATUS_LABELS } from '../utils/run'

const open = defineModel<boolean>({ required: true })
const emit = defineEmits<{ select: [runId: string] }>()
const page = shallowRef<RunList | null>(null)
const loading = ref(false)
const error = ref('')
let controller: AbortController | undefined

async function load(offset = 0): Promise<void> {
  controller?.abort()
  const request = new AbortController()
  controller = request
  loading.value = true
  error.value = ''
  try {
    const result = await harnessApi.listRuns(offset, request.signal)
    if (!request.signal.aborted) page.value = result
  } catch (cause) {
    if (!request.signal.aborted)
      error.value = cause instanceof Error ? cause.message : '运行记录读取失败'
  } finally {
    if (!request.signal.aborted) loading.value = false
  }
}
watch(
  open,
  (value) => {
    if (value) void load()
    else controller?.abort()
  },
  { immediate: true },
)
onBeforeUnmount(() => controller?.abort())
</script>

<template>
  <USlideover
    v-model:open="open"
    title="运行记录"
    description="当前服务保存的任务历史"
    :ui="{ content: 'history-overlay', header: 'inspector-header', body: 'inspector-body' }"
  >
    <template #body>
      <div class="details-content">
        <div class="board-row">
          <p>查看当前服务保存的运行。</p>
          <UButton :loading="loading" @click="load()" color="neutral" variant="outline"
            >刷新</UButton
          >
        </div>
        <UAlert v-if="error" :title="error" color="error" variant="subtle" />
        <p v-if="loading" role="status">正在读取运行记录…</p>
        <div v-for="item in page?.items ?? []" :key="item.run_id" class="detail-section">
          <div class="board-row">
            <strong>{{ item.engine === 'blackboard' ? '共享黑板' : '任务协作' }}</strong>
            <span>{{
              item.engine === 'blackboard' && item.status === 'waiting_human'
                ? '等待补充'
                : STATUS_LABELS[item.status]
            }}</span>
          </div>
          <p class="board-text">
            <code>{{ item.run_id }}</code>
          </p>
          <p>更新于 {{ displayTime(item.updated_at) }}</p>
          <UButton @click="emit('select', item.run_id)" color="neutral" variant="outline"
            >查看运行详情</UButton
          >
        </div>
        <p v-if="page && !page.items.length && !loading">当前页没有运行记录。</p>
        <div v-if="page" class="board-row">
          <UButton
            :disabled="loading || page.offset === 0"
            @click="load(Math.max(0, page.offset - page.limit))"
            color="neutral"
            variant="outline"
            >上一页</UButton
          >
          <span>第 {{ Math.floor(page.offset / page.limit) + 1 }} 页</span>
          <UButton
            :disabled="loading || page.items.length < page.limit"
            @click="load(page.offset + page.limit)"
            color="neutral"
            variant="outline"
            >下一页</UButton
          >
        </div>
      </div>
    </template>
  </USlideover>
</template>
