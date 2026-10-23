<script setup lang="ts">
import { onBeforeUnmount, ref, shallowRef, watch } from 'vue'
import { harnessApi } from '../services/api'
import type {
  BlackboardHistoryPage,
  BlackboardObjectDetail,
  BlackboardSummary,
  BoardObject,
  ObjectRef,
} from '../types'
import { displayTime } from '../utils/blackboard'
import { objectRef, refKey } from '../utils/knowledgeGraph'
import BlackboardObjectCard from './BlackboardObjectCard.vue'

const props = defineProps<{ runId: string; snapshot: BlackboardSummary; selected: ObjectRef }>()
const emit = defineEmits<{
  select: [ref: ObjectRef]
  locate: [revision: number]
  loaded: [object: BoardObject]
  close: []
}>()
const detail = shallowRef<BlackboardObjectDetail | null>(null)
const history = shallowRef<BlackboardHistoryPage | null>(null)
const loading = ref(false)
const moreLoading = ref(false)
const error = ref('')
const historyError = ref('')
let controller: AbortController | undefined

async function load(): Promise<void> {
  controller?.abort()
  const request = new AbortController()
  controller = request
  detail.value = null
  history.value = null
  error.value = ''
  historyError.value = ''
  loading.value = true
  moreLoading.value = false
  const [object, revisions] = await Promise.allSettled([
    harnessApi.blackboardObject(
      props.runId,
      props.selected,
      props.snapshot.revision,
      request.signal,
    ),
    harnessApi.blackboardHistory(
      props.runId,
      props.selected.object_id,
      { at_revision: props.snapshot.revision, limit: 20 },
      request.signal,
    ),
  ])
  if (request.signal.aborted) return
  loading.value = false
  if (object.status === 'fulfilled') {
    detail.value = object.value
    emit('loaded', object.value.item.object)
  } else error.value = object.reason instanceof Error ? object.reason.message : '节点读取失败'
  if (revisions.status === 'fulfilled') history.value = revisions.value
  else
    historyError.value =
      revisions.reason instanceof Error ? revisions.reason.message : '历史读取失败'
}

async function loadMore(): Promise<void> {
  if (!history.value?.next_cursor || !controller || moreLoading.value) return
  const request = controller
  moreLoading.value = true
  historyError.value = ''
  try {
    const page = await harnessApi.blackboardHistory(
      props.runId,
      props.selected.object_id,
      { cursor: history.value.next_cursor, limit: 20 },
      request.signal,
    )
    if (!request.signal.aborted && history.value)
      history.value = { ...page, items: [...history.value.items, ...page.items] }
  } catch (cause) {
    if (!request.signal.aborted)
      historyError.value = cause instanceof Error ? cause.message : '历史读取失败'
  } finally {
    if (!request.signal.aborted) moreLoading.value = false
  }
}

watch(
  () => [props.runId, props.snapshot.revision, refKey(props.selected)],
  () => {
    void load()
  },
  { immediate: true },
)
onBeforeUnmount(() => controller?.abort())
</script>

<template>
  <section class="graph-node-details" aria-label="知识节点详情">
    <div class="board-row">
      <h3>节点详情 · 对象第 {{ selected.revision }} 版</h3>
      <UButton variant="ghost" @click="emit('close')" color="neutral">关闭节点</UButton>
    </div>
    <p class="board-meta">对象 ID：{{ selected.object_id }}</p>
    <p v-if="loading" role="status">正在读取节点及历史…</p>
    <p v-if="error" role="alert">
      {{ error }} <button type="button" @click="load">重试节点</button>
    </p>
    <template v-if="detail">
      <p>
        提交来源：{{ detail.item.event.source_id }} ·
        {{ displayTime(detail.item.event.committed_at) }}
      </p>
      <p>此对象版本写入黑板第 {{ detail.item.event.sequence }} 版。</p>
      <UButton @click="emit('locate', detail.item.event.sequence)" color="neutral" variant="outline"
        >定位到当时的板面</UButton
      >
      <div v-if="detail.activation" class="board-note">
        <h4>
          {{ detail.item.event.activation_id ? '此修订关联的激活理由' : '对象产生时的激活理由' }}
        </h4>
        <p>{{ detail.activation.reason || '未记录理由' }}</p>
        <p>{{ detail.activation.source_id }} · {{ detail.activation.source_version }}</p>
        <details>
          <summary>激活所读取的对象版本</summary>
          <div class="board-references">
            <button
              v-for="item in detail.activation.input_refs"
              :key="refKey(item)"
              type="button"
              @click="emit('select', item)"
            >
              {{ item.object_id }} · 第 {{ item.revision }} 版
            </button>
          </div>
        </details>
      </div>
      <p v-else class="board-meta">没有可用的持久激活记录；以下展示已提交的来源和证据。</p>
      <p
        v-for="(change, index) in detail.item.event.requested_changes.filter(
          (item) => item.target.object_id === selected.object_id,
        )"
        :key="index"
      >
        变更原因：{{ change.reason }}
      </p>
      <p v-if="detail.item.event.goal_revision?.target.object_id === selected.object_id">
        目标修订原因：{{ detail.item.event.goal_revision.reason }}
      </p>
      <BlackboardObjectCard
        :key="`${snapshot.revision}:${refKey(selected)}`"
        :object="detail.item.object"
        :run-id="runId"
        :board-revision="detail.item.event.sequence"
        :evaluated-at="snapshot.evaluated_at"
        @inspect="emit('select', $event)"
        @graph="emit('select', $event)"
      />
    </template>
    <h4>对象历史</h4>
    <p class="board-meta">
      历史范围截至黑板第
      {{ snapshot.revision }} 版。选择对象版本查看内容，定位板面可还原当时的目标与关系。
    </p>
    <p v-if="historyError" role="alert">
      {{ historyError }} <button type="button" @click="load">重试历史</button>
    </p>
    <ol v-if="history" class="graph-history">
      <li v-for="item in history.items" :key="item.object.revision">
        <button
          type="button"
          :aria-pressed="item.object.revision === selected.revision"
          @click="emit('select', objectRef(item.object))"
        >
          对象第 {{ item.object.revision }} 版 · 黑板第 {{ item.event.sequence }} 版 ·
          {{ displayTime(item.event.committed_at) }}
        </button>
      </li>
    </ol>
    <UButton
      v-if="history?.next_cursor"
      :loading="moreLoading"
      @click="loadMore"
      color="neutral"
      variant="outline"
      >加载更多历史</UButton
    >
  </section>
</template>
