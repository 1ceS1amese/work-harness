<script setup lang="ts">
import { computed, defineAsyncComponent, onBeforeUnmount, ref, shallowRef, watch } from 'vue'
import { useBlackboardView } from '../composables/useBlackboardView'
import { harnessApi } from '../services/api'
import type { BoardObject, ObjectRef, RunResult } from '../types'
import { CLAIM_LABELS, displayTime, stopReason } from '../utils/blackboard'
import { getRunStage } from '../utils/run'
import BlackboardObjectCard from './BlackboardObjectCard.vue'
const BlackboardKnowledgeGraph = defineAsyncComponent(
  () => import('./BlackboardKnowledgeGraph.vue'),
)

const props = defineProps<{ run: RunResult }>()
const emit = defineEmits<{ update: [run: RunResult] }>()
const liveRun = shallowRef(props.run)
watch(
  () => props.run,
  (value) => {
    liveRun.value = value
  },
)
const {
  summary,
  page,
  kind,
  loading,
  error,
  monitorError,
  hasUpdates,
  follow,
  historicalRevision,
  refresh,
  viewRevision,
  showLatest,
  loadMore,
} = useBlackboardView(
  () => props.run,
  (value) => {
    liveRun.value = value
    emit('update', value)
  },
)
const view = ref<'objects' | 'graph'>('objects')
const graphFocus = shallowRef<ObjectRef | null>(null)
function setView(value: 'objects' | 'graph'): void {
  graphFocus.value = null
  view.value = value
  follow.value = value === 'graph' && historicalRevision.value === null
}
function showInGraph(ref: ObjectRef): void {
  view.value = 'graph'
  graphFocus.value = ref
  follow.value = false
}
async function locateGraphNode(revision: number, ref: ObjectRef): Promise<void> {
  await viewRevision(revision)
  if (summary.value?.revision === revision) graphFocus.value = { ...ref }
}
const stage = computed(() => getRunStage(liveRun.value))
const selection = shallowRef<BoardObject | null>(null)
const selectionError = ref('')
const selecting = ref(false)
let selectionRequest: AbortController | undefined

function clearSelection(): void {
  selectionRequest?.abort()
  selection.value = null
  selectionError.value = ''
  selecting.value = false
}
watch(() => summary.value?.revision, clearSelection)
onBeforeUnmount(clearSelection)

async function inspect(ref: ObjectRef): Promise<void> {
  if (view.value === 'graph') {
    showInGraph(ref)
    return
  }
  if (!summary.value) return
  clearSelection()
  selectionRequest = new AbortController()
  const request = selectionRequest
  selecting.value = true
  try {
    const response = await harnessApi.blackboardObject(
      props.run.run_id,
      ref,
      summary.value.revision,
      request.signal,
    )
    if (!request.signal.aborted) selection.value = response.item.object
  } catch (error) {
    if (!request.signal.aborted)
      selectionError.value = error instanceof Error ? error.message : '对象读取失败'
  } finally {
    if (!request.signal.aborted) selecting.value = false
  }
}
</script>

<template>
  <div class="details-content blackboard-details">
    <section class="detail-section">
      <div class="board-row">
        <div>
          <p class="detail-kicker">共享黑板 · 最新运行状态</p>
          <h3>{{ stage.label }}</h3>
        </div>
        <UButton :loading="loading" @click="refresh" color="neutral" variant="outline"
          >刷新黑板</UButton
        >
      </div>
      <p>{{ stage.description }}</p>
      <p>
        已记录 {{ liveRun.usage.input_tokens + liveRun.usage.output_tokens }} Token · 输入
        {{ liveRun.usage.input_tokens }} · 输出 {{ liveRun.usage.output_tokens }} · 输入中缓存命中
        {{ liveRun.usage.cached_tokens }}
      </p>
      <p v-if="liveRun.usage.usage_complete === false" class="board-note" role="status">
        部分调用用量尚未确认，以上为已知用量。
      </p>
      <p v-if="hasUpdates" class="board-note" role="status">
        黑板已有更新。点击“刷新黑板”查看最新目标、对象和证据。
      </p>
      <p v-if="monitorError" role="status">更新检查失败：{{ monitorError }}</p>
    </section>
    <UAlert v-if="error" :title="error" color="warning" variant="subtle" />
    <p v-if="loading && !summary" role="status">正在读取黑板…</p>
    <template v-if="summary">
      <div class="board-row graph-view-switch" aria-label="黑板视图">
        <div>
          <button type="button" :aria-pressed="view === 'objects'" @click="setView('objects')">
            目标与对象
          </button>
          <button type="button" :aria-pressed="view === 'graph'" @click="setView('graph')">
            知识关系图
          </button>
        </div>
        <label v-if="view === 'graph' && historicalRevision === null"
          ><input v-model="follow" type="checkbox" /> 自动跟随新提交</label
        >
      </div>
      <div v-if="historicalRevision !== null" class="board-note" role="status">
        正在查看历史板面。目标、对象与关系均按第 {{ summary.revision }} 版读取。
        <UButton @click="showLatest" color="neutral" variant="outline">返回最新板面</UButton>
      </div>
      <p v-else-if="view === 'graph' && !follow" class="board-meta">
        已暂停自动跟随，当前节点和版本保持不变。勾选自动跟随可继续更新。
      </p>
      <section class="detail-section">
        <p class="board-meta">
          当前查看黑板第 {{ summary.revision }} 版 · 评估时间
          {{ displayTime(summary.evaluated_at) }}
        </p>
        <h3>目标 · {{ summary.goal.satisfied ? '验收已满足' : '仍有验收缺口' }}</h3>
        <p class="board-text">{{ summary.root_intent.objective }}</p>
        <ul class="board-criteria">
          <li v-for="criterion in summary.root_intent.acceptance" :key="criterion.id">
            <strong>{{
              summary.goal.missing_criteria.includes(criterion.id) ? '待满足' : '已满足'
            }}</strong>
            <span>{{ criterion.description }}</span>
            <details>
              <summary>查看验收范围</summary>
              <p>{{ CLAIM_LABELS[criterion.claim_type] }} · {{ criterion.claim_key }}</p>
              <p>适用范围：{{ JSON.stringify(criterion.scope) }}</p>
              <p>
                条件：{{ criterion.operator
                }}<template v-if="criterion.operator !== 'exists'">
                  {{ JSON.stringify(criterion.expected) }}</template
                >
              </p>
              <p>要求的检查项：{{ criterion.required_checks.join('、') || '未指定' }}</p>
            </details>
          </li>
        </ul>
        <div v-if="summary.goal.witnesses.length" class="board-references">
          <h4>验收依据</h4>
          <button
            v-for="ref in summary.goal.witnesses"
            :key="`${ref.object_id}:${ref.revision}`"
            type="button"
            @click="inspect(ref)"
          >
            {{ ref.object_id }} · 第 {{ ref.revision }} 版
          </button>
        </div>
        <div v-if="summary.goal.conflicts.length" class="board-references">
          <h4>尚未解决的冲突</h4>
          <button
            v-for="ref in summary.goal.conflicts"
            :key="`${ref.object_id}:${ref.revision}`"
            type="button"
            @click="inspect(ref)"
          >
            {{ ref.object_id }} · 第 {{ ref.revision }} 版
          </button>
        </div>
      </section>
      <section v-if="summary.finalization" class="detail-section">
        <h3>停止原因 · {{ stopReason(summary.finalization.reason) }}</h3>
        <p class="board-text">{{ summary.finalization.output || '未返回文本结果。' }}</p>
        <template v-if="summary.finalization.missing_criteria.length">
          <h4>结束时尚缺</h4>
          <ul>
            <li v-for="id in summary.finalization.missing_criteria" :key="id">
              {{ summary.root_intent.acceptance.find((item) => item.id === id)?.description || id }}
            </li>
          </ul>
        </template>
        <template v-if="summary.finalization.attempted_directions.length">
          <h4>已尝试的方向</h4>
          <ul>
            <li v-for="direction in summary.finalization.attempted_directions" :key="direction">
              {{ direction }}
            </li>
          </ul>
        </template>
      </section>
      <BlackboardKnowledgeGraph
        v-if="view === 'graph'"
        :run-id="run.run_id"
        :snapshot="summary"
        :focus="graphFocus"
        @pause="follow = false"
        @locate="locateGraphNode"
      />
      <section
        v-if="view === 'objects' && (selecting || selection || selectionError)"
        class="detail-section"
        aria-label="所选依据"
      >
        <div class="board-row">
          <h3>所选依据</h3>
          <UButton variant="ghost" @click="clearSelection" color="neutral">关闭</UButton>
        </div>
        <p v-if="selecting" role="status">正在读取依据…</p>
        <p v-if="selectionError" role="alert">{{ selectionError }}</p>
        <BlackboardObjectCard
          v-if="selection"
          :key="`${summary.revision}:${selection.id}:${selection.revision}`"
          :object="selection"
          :run-id="run.run_id"
          :board-revision="summary.revision"
          :evaluated-at="summary.evaluated_at"
          @inspect="inspect"
          @graph="showInGraph"
        />
      </section>
      <section v-if="view === 'objects'" class="detail-section">
        <h3>共享对象</h3>
        <p>Fact 保存有核验依据的主张；Intent 记录待探索的方向；Hint 提供经验或假设。</p>
        <p class="board-meta">
          事实记录 {{ summary.counts.fact }} · 探索方向 {{ summary.counts.intent }} · 经验提示
          {{ summary.counts.hint }}（含历史状态）
        </p>
        <label class="board-filter"
          >对象类型
          <select v-model="kind">
            <option value="">全部对象</option>
            <option value="fact">Fact · 事实记录</option>
            <option value="intent">Intent · 探索方向</option>
            <option value="hint">Hint · 经验提示</option>
          </select>
        </label>
        <div class="board-objects" :aria-busy="loading">
          <BlackboardObjectCard
            v-for="item in page?.items ?? []"
            :key="`${summary.revision}:${item.id}:${item.revision}`"
            :object="item"
            :run-id="run.run_id"
            :board-revision="summary.revision"
            :evaluated-at="summary.evaluated_at"
            @inspect="inspect"
            @graph="showInGraph"
          />
        </div>
        <p v-if="page && !page.items.length && !loading">当前筛选下没有对象。</p>
        <div v-if="page" class="board-row board-pagination">
          <span>已展示 {{ page.items.length }} / {{ page.total }} 个对象</span>
          <UButton
            v-if="page.next_cursor"
            :loading="loading"
            @click="loadMore"
            color="neutral"
            variant="outline"
            >加载更多</UButton
          >
        </div>
      </section>
    </template>
    <UAlert
      v-for="issue in liveRun.errors"
      :key="`${issue.code}:${issue.occurred_at}`"
      :title="issue.message"
      color="error"
      variant="subtle"
    />
  </div>
</template>
