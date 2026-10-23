<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { harnessApi } from '../services/api'
import type { ModelDiscoveryRequest, ModelSettings } from '../types'

const settings = defineModel<Record<string, ModelSettings>>({ required: true })
const props = defineProps<{ models: string[]; connection: ModelDiscoveryRequest }>()
const emit = defineEmits<{ busy: [value: boolean] }>()
const commonEfforts = ['none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max']
const model = ref('')
const extraEfforts = ref('')
const confirmedEfforts = ref('')
const testing = ref(false)
const error = ref('')
let controller: AbortController | undefined
const current = computed(() => settings.value[model.value])
const supported = computed(
  () =>
    current.value?.verification.checks
      .filter((item) => item.effort !== null && item.status === 'supported')
      .map((item) => item.effort!) ?? [],
)
const defaultEffort = computed({
  get: () => current.value?.default_effort ?? '',
  set: (value: string) => {
    if (!current.value) return
    settings.value = {
      ...settings.value,
      [model.value]: { ...current.value, default_effort: value || null },
    }
  },
})
const statuses = {
  supported: '通过',
  unsupported: '不支持',
  unverified: '未确认',
  error: '暂未完成',
}
function cancel() {
  controller?.abort()
  testing.value = false
  emit('busy', false)
  error.value = ''
}
watch(
  () => props.models,
  (models) => {
    if (!models.includes(model.value)) model.value = models[0] ?? ''
  },
  { immediate: true },
)
watch(() => JSON.stringify(props.connection), cancel)
watch(model, cancel)
watch(
  () => current.value?.verification.id,
  () => {
    extraEfforts.value =
      current.value?.verification.checks
        .filter((item) => item.effort && !commonEfforts.includes(item.effort))
        .map((item) => item.effort)
        .join(', ') ?? ''
    confirmedEfforts.value =
      current.value?.verification.checks
        .filter((item) => item.source === 'channel_confirmation' && item.status === 'supported')
        .map((item) => item.effort)
        .join(', ') ?? ''
  },
  { immediate: true },
)
onBeforeUnmount(cancel)
async function verify() {
  cancel()
  const efforts = [
    ...new Set([...commonEfforts, ...extraEfforts.value.split(/[\s,，]+/).filter(Boolean)]),
  ]
  if (efforts.length > 12 || efforts.some((value) => !/^[a-z][a-z0-9_-]{0,31}$/.test(value))) {
    error.value = '档位使用小写字母、数字、- 或 _，最多测试 12 种。'
    return
  }
  const confirmed = confirmedEfforts.value.split(/[\s,，]+/).filter(Boolean)
  if (confirmed.some((value) => !efforts.includes(value))) {
    error.value = '渠道确认的档位必须包含在本次测试档位中。'
    return
  }
  const target = model.value
  const active = new AbortController()
  controller = active
  testing.value = true
  emit('busy', true)
  try {
    const result = await harnessApi.verifyModel(
      { ...props.connection, model: target, efforts, confirmed_efforts: confirmed },
      active.signal,
    )
    if (active.signal.aborted) return
    const previous = settings.value[target]?.default_effort
    settings.value = {
      ...settings.value,
      [target]: {
        verification: result,
        default_effort:
          previous &&
          result.checks.some((item) => item.effort === previous && item.status === 'supported')
            ? previous
            : null,
      },
    }
  } catch (cause) {
    if (!active.signal.aborted)
      error.value = cause instanceof Error ? cause.message : '测试失败，请稍后重试'
  } finally {
    if (!active.signal.aborted) {
      testing.value = false
      emit('busy', false)
    }
  }
}
</script>

<template>
  <section v-if="models.length" class="reasoning-settings" aria-label="模型推理配置">
    <div class="reasoning-heading">
      <div>
        <h3>推理配置</h3>
        <p>先测试当前模型，再选择通过验证的档位。保存并重启后生效。</p>
      </div>
      <UButton
        color="neutral"
        variant="outline"
        :loading="testing"
        :disabled="testing || !model"
        @click="verify"
        >测试模型与推理</UButton
      >
    </div>
    <div class="provider-form-grid">
      <label class="native-form-field"
        ><span>测试模型</span>
        <select v-model="model" aria-label="测试模型">
          <option v-for="item in models" :key="item" :value="item">{{ item }}</option>
        </select>
      </label>
      <label class="native-form-field"
        ><span>默认推理档位</span>
        <select
          v-model="defaultEffort"
          aria-label="默认推理档位"
          :disabled="testing || !supported.length"
        >
          <option value="">默认（不指定）</option>
          <option v-for="effort in supported" :key="effort" :value="effort">{{ effort }}</option>
        </select>
      </label>
    </div>
    <p v-if="testing" role="status">正在测试默认调用和推理参数，通常需要数十秒…</p>
    <p v-else-if="!current" role="status">尚未测试。通过后可在聊天框选择此模型。</p>
    <p v-else role="status">
      {{ current.verification.model_available ? '模型调用已通过' : '模型调用尚未通过' }} ·
      {{ new Date(current.verification.checked_at).toLocaleString('zh-CN') }}
    </p>
    <p v-if="error" role="alert" class="reasoning-error">{{ error }}</p>
    <details v-if="current" class="provider-advanced">
      <summary>测试结果 · {{ supported.length }} 个可选档位</summary>
      <ul class="reasoning-results">
        <li v-for="check in current.verification.checks" :key="check.effort ?? 'default'">
          <strong>{{ check.effort ?? '默认调用' }}</strong
          ><span>{{
            check.source === 'channel_confirmation' ? '渠道已确认' : statuses[check.status]
          }}</span
          ><small>{{ check.detail }}</small>
        </li>
      </ul>
      <p>测试确认当前渠道的参数兼容性，不衡量推理质量。未确认的档位保持不可选。</p>
    </details>
    <details class="provider-advanced">
      <summary>更多验证选项</summary>
      <label class="native-form-field"
        ><span>额外测试档位</span
        ><input
          v-model="extraEfforts"
          placeholder="上游支持的其他名称，用逗号分隔"
          :disabled="testing"
      /></label>
      <label class="native-form-field">
        <span>渠道已确认的档位（可选）</span>
        <input v-model="confirmedEfforts" placeholder="例如 low, high, xhigh" :disabled="testing" />
        <small>仅填写渠道已明确确认的档位；仍需实际调用通过，结果会注明确认来源。</small>
      </label>
    </details>
  </section>
</template>
