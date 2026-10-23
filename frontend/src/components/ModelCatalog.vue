<script setup lang="ts">
import { computed, ref } from 'vue'
import type { UpstreamModel } from '../types'

const props = defineProps<{
  items: UpstreamModel[]
  selected: string[]
  loading: boolean
  error: string
  fetched: boolean
  truncated: boolean
}>()
const emit = defineEmits<{ refresh: []; toggle: [id: string] }>()
const search = ref('')
const matches = computed(() => {
  const query = search.value.trim().toLowerCase()
  return props.items.filter((item) => `${item.id} ${item.name}`.toLowerCase().includes(query))
})
// Keep large gateway catalogs responsive; search still covers the full list.
const visible = computed(() => matches.value.slice(0, 100))
</script>

<template>
  <div class="model-catalog">
    <div class="model-catalog-toolbar">
      <div><strong>上游模型</strong><small>使用当前连接与密钥读取，无需先保存。</small></div>
      <UButton
        icon="i-lucide-cloud-download"
        color="neutral"
        variant="outline"
        :loading="loading"
        @click="emit('refresh')"
        >获取模型</UButton
      >
    </div>
    <UAlert v-if="error" color="error" variant="subtle" title="无法获取模型" :description="error" />
    <template v-if="fetched">
      <UInput
        v-if="items.length"
        v-model="search"
        icon="i-lucide-search"
        aria-label="搜索上游模型"
        placeholder="搜索模型 ID 或名称"
        class="w-full"
      />
      <p role="status" class="model-catalog-status">
        已获取 {{ items.length }} 个模型 · 已选 {{ selected.length }} 个<span v-if="truncated"
          >（列表已截断，可手动添加其他模型）</span
        >
      </p>
      <div v-if="visible.length" class="model-catalog-list" aria-label="上游模型列表">
        <label
          v-for="item in visible"
          :key="item.id"
          class="model-catalog-item"
          :class="{ selected: selected.includes(item.id) }"
        >
          <input
            type="checkbox"
            :checked="selected.includes(item.id)"
            @change="emit('toggle', item.id)"
          />
          <span
            ><strong>{{ item.id }}</strong
            ><small v-if="item.name !== item.id">{{ item.name }}</small></span
          >
          <UIcon v-if="selected.includes(item.id)" name="i-lucide-check" />
        </label>
      </div>
      <p v-else class="model-catalog-status">
        {{
          items.length
            ? '没有匹配的模型，请调整搜索内容。'
            : '上游返回了空列表，可检查密钥权限或手动添加模型 ID。'
        }}
      </p>
      <p v-if="matches.length > 100" class="model-catalog-status">
        显示前 100 项，可搜索其余模型。
      </p>
    </template>
    <p class="model-catalog-note">
      模型列表不代表所有模型都支持当前协议。请使用渠道已开放的协议与模型；未开放列表接口时可手动添加。
    </p>
  </div>
</template>
