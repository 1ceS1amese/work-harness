<script setup lang="ts">
import { onBeforeUnmount, reactive } from 'vue'
import { harnessApi } from '../services/api'
import type { BoardObject, ContentRef, ObjectRef } from '../types'
import { CLAIM_LABELS, displayTime, KIND_LABELS, objectStatus, objectText } from '../utils/blackboard'

const props = defineProps<{ object: BoardObject; runId: string; boardRevision: number; evaluatedAt: string }>()
const emit = defineEmits<{ inspect: [ref: ObjectRef]; graph: [ref: ObjectRef] }>()
const content = reactive<Record<string, { body?: string; error?: string; loading: boolean }>>({})
const controller = new AbortController()
onBeforeUnmount(() => controller.abort())

async function openContent(ref: ContentRef): Promise<void> {
  const key = ref.content_digest
  if (content[key]?.loading || content[key]?.body !== undefined) return
  content[key] = { loading: true }
  try {
    const response = await harnessApi.blackboardContent(props.runId, key, props.boardRevision, controller.signal)
    if (!controller.signal.aborted) content[key] = { body: response.content.body, loading: false }
  } catch (error) {
    if (!controller.signal.aborted) content[key] = { error: error instanceof Error ? error.message : '内容读取失败', loading: false }
  }
}

function validityNotice(): string {
  const item = props.object
  const at = Date.parse(props.evaluatedAt)
  if (item.kind === 'fact') {
    if (item.valid_from && Date.parse(item.valid_from) > at) return '查看时尚未到生效时间。'
    if (item.valid_until && Date.parse(item.valid_until) <= at) return '查看时已超过有效期。'
  }
  if (item.kind === 'hint' && item.expires_at && Date.parse(item.expires_at) <= at) return '查看时提示已过期。'
  return ''
}
</script>

<template>
  <article class="board-object" :data-kind="object.kind">
    <div class="board-row">
      <strong>{{ KIND_LABELS[object.kind] }}</strong>
      <span class="board-state">{{ objectStatus(object) }}</span>
    </div>
    <p class="board-text">{{ objectText(object) }}</p>
    <p v-if="object.kind === 'hint'" class="board-note">经验或假设，尚未核验；置信度不代表已确认。</p>
    <p v-if="object.kind === 'fact' && object.claim_type === 'user_statement'" class="board-note">
      已确认用户作出此陈述，陈述内容未被独立核验。
    </p>
    <p v-if="object.kind === 'fact' && object.claim_type === 'assessment'" class="board-note">
      这是一项评估意见；核验范围以证据和检查项为准。
    </p>
    <p v-if="object.kind === 'fact' && object.scope.verification === 'stored_content_only'" class="board-note">
      仅确认内容已保存，不代表内容质量、外部事实或操作执行已核验。
    </p>
    <p v-if="validityNotice()" class="board-note">{{ validityNotice() }}</p>
    <p class="board-meta">{{ object.created_by }} · {{ object.topic }} · 对象第 {{ object.revision }} 版</p>
    <button class="graph-object-link" type="button" @click="emit('graph', { object_id: object.id, revision: object.revision })">在关系图中定位</button>
    <details>
      <summary>来源、证据与内容</summary>
      <dl class="board-fields">
        <dt>对象</dt><dd><code>{{ object.id }}</code></dd>
        <dt>记录时间</dt><dd>{{ displayTime(object.created_at) }}</dd>
        <dt>适用范围</dt><dd>{{ Object.keys(object.scope).length ? JSON.stringify(object.scope) : '未指定' }}</dd>
        <dt>内容摘要</dt><dd><code>{{ object.content_digest }}</code></dd>
      </dl>
      <template v-if="object.kind === 'fact'">
        <h4>核验范围 · {{ CLAIM_LABELS[object.claim_type] }}</h4>
        <p>核验记录与时间条件、前提有效性共同决定可用性，验收以目标评估为准。</p>
        <dl class="board-fields">
          <dt>主张</dt><dd>{{ object.claim_key || '未指定' }}</dd>
          <dt>值</dt><dd><pre class="board-pre">{{ JSON.stringify(object.value, null, 2) }}</pre></dd>
          <dt>核验方</dt><dd>{{ object.verification.verifier_id }} · {{ object.verification.verifier_version }}</dd>
          <dt>方法</dt><dd>{{ object.verification.method }}</dd>
          <dt>检查项</dt><dd>{{ object.verification.checks.join('、') || '未指定' }}</dd>
          <dt>核验时间</dt><dd>{{ displayTime(object.verification.verified_at) }}</dd>
          <dt>有效时间</dt><dd>{{ object.valid_from ? displayTime(object.valid_from) : '无起始限制' }} — {{ object.valid_until ? displayTime(object.valid_until) : '无截止限制' }}</dd>
        </dl>
        <h4>证据</h4>
        <ul class="board-evidence">
          <li v-for="(evidence, index) in object.evidence" :key="index">
            <p>{{ evidence.description }}</p>
            <span>{{ evidence.source }} · {{ evidence.locator }}</span>
            <code>{{ evidence.content_digest }}</code>
          </li>
        </ul>
        <div v-if="object.verification.premise_refs.length" class="board-references">
          <span>核验前提</span>
          <button v-for="ref in object.verification.premise_refs" :key="`${ref.object_id}:${ref.revision}`" type="button" @click="emit('inspect', ref)">
            {{ ref.object_id }} · 第 {{ ref.revision }} 版
          </button>
        </div>
      </template>
      <template v-else-if="object.kind === 'intent'">
        <h4>验收条件</h4>
        <ul><li v-for="criterion in object.acceptance" :key="criterion.id">{{ criterion.description }}</li></ul>
        <p v-if="object.status_reason">状态原因：{{ object.status_reason }}</p>
        <div v-if="object.blockers.length" class="board-references">
          <span>阻塞依据</span>
          <button v-for="ref in object.blockers" :key="`${ref.object_id}:${ref.revision}`" type="button" @click="emit('inspect', ref)">{{ ref.object_id }} · 第 {{ ref.revision }} 版</button>
        </div>
      </template>
      <template v-else>
        <h4>提示的适用条件与理由</h4>
        <p>{{ object.applies_when }}</p><p>{{ object.rationale }}</p>
        <p v-if="object.confidence !== null">来源自报置信度：{{ Math.round(object.confidence * 100) }}%</p>
        <p v-if="object.expires_at">到期时间：{{ displayTime(object.expires_at) }}</p>
        <p v-if="object.status_reason">状态原因：{{ object.status_reason }}</p>
      </template>
      <div v-if="object.attachments.length" class="board-attachments">
        <h4>已提交的内容</h4>
        <div v-for="attachment in object.attachments" :key="attachment.content_digest">
          <button type="button" :disabled="content[attachment.content_digest]?.loading" @click="openContent(attachment)">
            查看 {{ attachment.media_type }} · {{ attachment.size_bytes }} 字节
          </button>
          <code>{{ attachment.content_digest }}</code>
          <p v-if="content[attachment.content_digest]?.error" role="alert">{{ content[attachment.content_digest]?.error }}，可点击重试。</p>
          <pre v-if="content[attachment.content_digest]?.body !== undefined" class="board-pre">{{ content[attachment.content_digest]?.body }}</pre>
        </div>
      </div>
    </details>
  </article>
</template>
