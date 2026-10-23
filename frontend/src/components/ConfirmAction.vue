<script setup lang="ts">
import { ref } from 'vue'
defineProps<{ title: string; confirmButtonText?: string; cancelButtonText?: string }>()
const emit = defineEmits<{ confirm: [] }>()
const open = ref(false)
function confirm(): void {
  emit('confirm')
  open.value = false
}
</script>
<template>
  <UPopover v-model:open="open">
    <slot name="reference" />
    <template #content>
      <div class="confirmation-popover">
        <p>{{ title }}</p>
        <div>
          <UButton color="neutral" variant="ghost" @click="open = false">{{
            cancelButtonText || '取消'
          }}</UButton
          ><UButton
            color="error"
            @click="confirm"
            >{{ confirmButtonText || '确认' }}</UButton
          >
        </div>
      </div>
    </template>
  </UPopover>
</template>
