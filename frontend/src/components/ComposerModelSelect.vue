<script setup lang="ts">
import { computed } from 'vue'
import type { ModelChoice, ModelSelection } from '../types'
const selected = defineModel<ModelSelection | null>({ default: null })
const props = defineProps<{ choices: ModelChoice[]; disabled: boolean }>()
const emit = defineEmits<{ settings: [] }>()
const projectDefault = 'project-default'
const key = (item: ModelSelection) => JSON.stringify([item.provider, item.model])
const unavailable = computed(
  () => selected.value && !props.choices.some((item) => key(item) === key(selected.value!)),
)
function label(choice: ModelChoice): string {
  const duplicate = props.choices.filter((item) => item.model === choice.model).length > 1
  return `${choice.model} · ${choice.reasoning_effort ?? '默认'}${duplicate ? ` · ${choice.provider}` : ''}`
}
const items = computed(() => [
  { value: projectDefault, label: '跟随项目设置' },
  ...(unavailable.value && selected.value
    ? [
        {
          value: key(selected.value),
          label: `${selected.value.model}（需重新测试）`,
          disabled: true,
        },
      ]
    : []),
  ...props.choices.map((choice) => ({ value: key(choice), label: label(choice) })),
])
const value = computed({
  get: () => (selected.value ? key(selected.value) : projectDefault),
  set: (value: string) => {
    const choice = props.choices.find((item) => key(item) === value)
    selected.value = choice ? { provider: choice.provider, model: choice.model } : null
  },
})
</script>

<template>
  <div class="composer-model-control">
    <UIcon name="i-lucide-zap" class="model-zap-icon" aria-hidden="true" />
    <USelect
      v-model="value"
      :items="items"
      class="composer-model-select"
      color="neutral"
      variant="ghost"
      size="sm"
      aria-label="当前任务模型"
      :disabled="disabled"
      :title="selected ? `${selected.provider} / ${selected.model}` : '跟随项目的模型分配'"
      :content="{ side: 'top', align: 'end', sideOffset: 8, collisionPadding: 12 }"
      :ui="{
        content: 'composer-model-menu',
        value: 'composer-model-value',
        item: 'composer-model-item',
        itemLabel: 'composer-model-item-label',
        itemTrailingIcon: 'composer-model-check',
      }"
    />
    <button
      v-if="!choices.length || unavailable"
      class="composer-model-settings"
      type="button"
      :disabled="disabled"
      @click="emit('settings')"
    >
      配置模型
    </button>
  </div>
</template>
