<script setup lang="ts">
import { ref, watch, onBeforeUnmount } from 'vue'
import { useAuthStore } from '../stores/auth'
import { useChatStore } from '../stores/chat'
import * as api from '../api/usage'
const auth = useAuthStore(), chat = useChatStore()
const data = ref<api.Allowance | null>(null)
let version = 0
watch(() => [auth.user?.id, chat.streaming], async () => {
  const current = ++version; data.value = null
  if (!auth.user || chat.streaming) return
  try { const result = await api.allowance(); if (current === version) data.value = result } catch { /* Chat transport gives actionable errors. */ }
}, { immediate: true })
onBeforeUnmount(() => { version++ })
</script>
<template>
  <small v-if="data" class="allowance-status" aria-live="polite">
    {{ data.paused ? '模型调用已暂停' : `今日剩余 ${data.remaining_questions}/${data.limits.user_daily_questions} 次 · 零点重置` }}
  </small>
</template>
<style scoped>.allowance-status { display: block; color: var(--text-muted, #aab9c7); padding: 6px 0; }</style>
