<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, shallowRef, watch } from 'vue'
import type { BlackboardSummary, BoardObject, ObjectRef } from '../types'
import { KIND_LABELS, objectStatus } from '../utils/blackboard'
import {
  loadGraphObjects,
  NODE_HEIGHT,
  NODE_WIDTH,
  nodeTitle,
  objectRef,
  projectKnowledgeGraph,
  refKey,
  RELATION_LABELS,
} from '../utils/knowledgeGraph'
import type { KnowledgeEdge, KnowledgeNode } from '../utils/knowledgeGraph'
import BlackboardNodeDetails from './BlackboardNodeDetails.vue'

const props = defineProps<{
  runId: string
  snapshot: BlackboardSummary
  focus?: ObjectRef | null
}>()
const emit = defineEmits<{ pause: []; locate: [revision: number, ref: ObjectRef] }>()
const objects = shallowRef<BoardObject[]>([])
const history = shallowRef<BoardObject[]>([])
const selected = shallowRef<ObjectRef | null>(null)
const loading = ref(false)
const error = ref('')
const search = ref('')
const zoom = ref(1)
const viewport = ref<HTMLElement | null>(null)
const detailsPanel = ref<HTMLElement | null>(null)
let controller: AbortController | undefined
const graph = computed(() =>
  projectKnowledgeGraph(objects.value, objectRef(props.snapshot.root_intent), history.value),
)
const nodeMap = computed(() => new Map(graph.value.nodes.map((node) => [node.key, node])))
const selectedKey = computed(() => (selected.value ? refKey(selected.value) : ''))
const connected = computed(() =>
  graph.value.edges.filter(
    (edge) => edge.source === selectedKey.value || edge.target === selectedKey.value,
  ),
)
const related = computed(
  () => new Set(connected.value.flatMap((edge) => [edge.source, edge.target])),
)
const matches = computed(() =>
  search.value.trim()
    ? graph.value.nodes.filter((node) =>
        `${nodeTitle(node)} ${node.ref.object_id}`
          .toLowerCase()
          .includes(search.value.trim().toLowerCase()),
      )
    : [],
)

async function load(): Promise<void> {
  controller?.abort()
  const request = new AbortController()
  controller = request
  loading.value = true
  error.value = ''
  objects.value = []
  history.value = []
  try {
    const result = await loadGraphObjects(props.runId, props.snapshot, request.signal)
    if (!request.signal.aborted) objects.value = result
  } catch (cause) {
    if (!request.signal.aborted)
      error.value = cause instanceof Error ? cause.message : '图数据读取失败'
  } finally {
    if (!request.signal.aborted) loading.value = false
  }
}
watch(
  () => [props.runId, props.snapshot.revision],
  () => {
    selected.value = null
    void load()
  },
  { immediate: true },
)
onBeforeUnmount(() => controller?.abort())
watch(
  () => props.focus,
  (value) => {
    if (value) select(value)
  },
  { immediate: true },
)

function select(ref: ObjectRef): void {
  emit('pause')
  selected.value = ref
  void nextTick(() => detailsPanel.value?.scrollIntoView?.({ block: 'nearest' }))
}
function remember(object: BoardObject): void {
  if (!objects.value.some((item) => refKey(objectRef(item)) === refKey(objectRef(object)))) {
    history.value = [
      ...history.value.filter((item) => refKey(objectRef(item)) !== refKey(objectRef(object))),
      object,
    ]
  }
}
function fit(): void {
  zoom.value = Math.max(0.25, Math.min(1, (viewport.value?.clientWidth ?? 600) / graph.value.width))
}
function lines(node: KnowledgeNode): string[] {
  const chars = Array.from(nodeTitle(node))
  const chunks = [
    chars.slice(0, 14).join(''),
    chars.slice(14, 28).join(''),
    chars.slice(28, 42).join(''),
  ]
  if (chars.length > 42) chunks[2] += '…'
  return chunks.filter(Boolean)
}
function path(edge: KnowledgeEdge): string {
  const from = nodeMap.value.get(edge.source)!
  const to = nodeMap.value.get(edge.target)!
  if (edge.source === edge.target)
    return `M ${from.x + 60} ${from.y} C ${from.x + 20} ${from.y - 30}, ${from.x + 170} ${from.y - 30}, ${from.x + 150} ${from.y}`
  if (from.x === to.x) {
    const x = from.x + NODE_WIDTH
    return `M ${x} ${from.y + 50} C ${x + 70} ${from.y + 50}, ${x + 70} ${to.y + 50}, ${x} ${to.y + 50}`
  }
  const forward = from.x < to.x
  const start = from.x + (forward ? NODE_WIDTH : 0)
  const end = to.x + (forward ? 0 : NODE_WIDTH)
  const middle = (start + end) / 2
  return `M ${start} ${from.y + 55} C ${middle} ${from.y + 55}, ${middle} ${to.y + 55}, ${end} ${to.y + 55}`
}
</script>

<template>
  <section class="detail-section knowledge-graph" aria-label="知识关系图">
    <div class="board-row">
      <h3>知识关系图</h3>
      <UButton :loading="loading" @click="load" color="neutral" variant="outline"
        >重试图数据</UButton
      >
    </div>
    <p>
      从来源沿箭头追溯产生、核验和目标贡献。支持与反驳关系不代表验证通过。点击节点查看依据与历史。
    </p>
    <p class="board-meta">
      黑板第 {{ snapshot.revision }} 版 · {{ objects.length }} 个当前对象 ·
      {{ history.length }} 个已展开历史版本 ·
      {{ graph.nodes.filter((node) => !node.object).length }} 个待读取引用
    </p>
    <p v-if="loading" role="status">正在读取完整关系图…</p>
    <p v-if="error" role="alert">{{ error }}</p>
    <template v-if="!loading && !error">
      <div class="graph-controls">
        <label>查找节点 <input v-model="search" type="search" placeholder="内容或对象 ID" /></label>
        <button type="button" @click="zoom = Math.max(0.25, zoom - 0.15)">缩小</button>
        <button type="button" @click="zoom = Math.min(2, zoom + 0.15)">放大</button>
        <button type="button" @click="fit">适应宽度</button>
        <span>{{ Math.round(zoom * 100) }}%</span>
      </div>
      <div v-if="search.trim()" class="graph-search-results">
        <p>找到 {{ matches.length }} 个节点</p>
        <button v-for="node in matches" :key="node.key" type="button" @click="select(node.ref)">
          {{ nodeTitle(node) }} · 第 {{ node.ref.revision }} 版
        </button>
      </div>
      <div class="graph-legend">
        <span>Fact 事实</span><span>Intent 方向</span><span>Hint 提示</span
        ><span>虚线边框：未读取的历史引用，点击展开</span>
      </div>
      <p v-if="graph.causalCycle" role="alert">
        产生关系出现环，无法按来源完整分层；请核对原始关系。
      </p>
      <div ref="viewport" class="graph-viewport" tabindex="0" aria-label="可滚动的知识关系图">
        <svg
          :viewBox="`0 0 ${graph.width} ${graph.height}`"
          :width="graph.width * zoom"
          :height="graph.height * zoom"
          role="group"
          aria-label="对象及其关系"
        >
          <defs>
            <marker
              id="knowledge-arrow"
              viewBox="0 0 10 10"
              refX="9"
              refY="5"
              markerWidth="7"
              markerHeight="7"
              orient="auto-start-reverse"
            >
              <path d="M 0 0 L 10 5 L 0 10 z" />
            </marker>
          </defs>
          <g
            v-for="edge in graph.edges"
            :key="edge.key"
            class="knowledge-edge"
            :class="{
              highlighted:
                selectedKey && (edge.source === selectedKey || edge.target === selectedKey),
              conflict: edge.relation === 'contradicts',
            }"
          >
            <title>{{ RELATION_LABELS[edge.relation] }} · {{ edge.relation }}</title>
            <path :d="path(edge)" marker-end="url(#knowledge-arrow)" />
          </g>
          <g
            v-for="node in graph.nodes"
            :key="node.key"
            class="knowledge-node"
            :class="{
              selected: selectedKey === node.key,
              related: related.has(node.key),
              unresolved: !node.object,
              root: node.root,
            }"
            :data-kind="node.object?.kind"
            :data-object-id="node.ref.object_id"
            :data-object-revision="node.ref.revision"
            :transform="`translate(${node.x}, ${node.y})`"
            role="button"
            tabindex="0"
            :aria-label="`${nodeTitle(node)}，对象第 ${node.ref.revision} 版`"
            :aria-pressed="selectedKey === node.key"
            @click="select(node.ref)"
            @keydown.enter.prevent="select(node.ref)"
            @keydown.space.prevent="select(node.ref)"
          >
            <title>
              {{ nodeTitle(node) }} · {{ node.ref.object_id }} · 第 {{ node.ref.revision }} 版
            </title>
            <rect
              :width="NODE_WIDTH"
              :height="NODE_HEIGHT"
              :rx="node.object?.kind === 'intent' ? 14 : 2"
            />
            <text x="12" y="21" class="graph-node-kind">
              {{
                node.root
                  ? '根目标'
                  : node.object
                    ? KIND_LABELS[node.object.kind]
                    : '历史引用 · 待读取'
              }}
              · v{{ node.ref.revision }}
            </text>
            <text v-for="(line, index) in lines(node)" :key="index" x="12" :y="44 + index * 16">
              {{ line }}
            </text>
            <text x="12" y="105" class="graph-node-status">
              {{ node.object ? objectStatus(node.object) : '点击读取准确版本'
              }}{{ node.object && !node.current ? ' · 历史' : '' }}
            </text>
          </g>
        </svg>
      </div>
      <div v-if="selected" class="graph-connected">
        <h4>与所选节点相连的关系 · {{ connected.length }}</h4>
        <p v-if="!connected.length">当前已读取的对象中没有与此版本相连的关系。</p>
        <ul>
          <li v-for="edge in connected" :key="edge.key">
            <button type="button" @click="select(nodeMap.get(edge.source)!.ref)">
              {{ nodeTitle(nodeMap.get(edge.source)!) }}
            </button>
            <span> → {{ RELATION_LABELS[edge.relation] }} → </span>
            <button type="button" @click="select(nodeMap.get(edge.target)!.ref)">
              {{ nodeTitle(nodeMap.get(edge.target)!) }}
            </button>
            <small
              >字段：{{ edge.relation }}；记录于 {{ edge.owner.object_id }} 第
              {{ edge.owner.revision }} 版</small
            >
          </li>
        </ul>
      </div>
      <div v-if="selected" ref="detailsPanel">
        <BlackboardNodeDetails
          :run-id="runId"
          :snapshot="snapshot"
          :selected="selected"
          @select="select"
          @loaded="remember"
          @locate="emit('locate', $event, selected)"
          @close="selected = null"
        />
      </div>
    </template>
  </section>
</template>
