<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { useChatStore } from '../stores/chat'
import type { Message } from '../types'
import ChatMessage from './ChatMessage.vue'
import ChatInput from './ChatInput.vue'

const chat = useChatStore()
const listEl = ref<HTMLElement | null>(null)

const greetingMessage = computed<Message | null>(() => {
  if (chat.messages.length > 0 || !chat.activeCharacter) return null
  return { id: -1, role: 'assistant', content: chat.activeCharacter.greeting, created_at: '' }
})

watch(
  () => chat.streamBuffer + String(chat.messages.length),
  async () => {
    await nextTick()
    if (listEl.value) listEl.value.scrollTop = listEl.value.scrollHeight
  },
)

async function startNew() {
  await chat.newSession()
}

function onSessionChange(event: Event) {
  const value = (event.target as HTMLSelectElement).value
  if (value) chat.openSession(value)
}
</script>

<template>
  <main class="chat-view" data-od-id="chat-view">
    <header class="chat-header">
      <div class="chat-header-title">
        <h1>{{ chat.activeCharacter?.display_name ?? '飞行雪绒' }}</h1>
        <p v-if="chat.activeSessionId" class="session-id">{{ chat.activeSessionId }}</p>
      </div>
      <div class="chat-header-right">
        <span class="status"><i aria-hidden="true"></i> 在线</span>
        <div class="mobile-controls">
          <button class="ghost" @click="startNew">新建</button>
          <select
            :value="chat.activeSessionId ?? ''"
            aria-label="切换会话"
            @change="onSessionChange"
          >
            <option value="" disabled>选择会话</option>
            <option v-for="s in chat.sessions" :key="s.id" :value="s.id">{{ s.title }}</option>
          </select>
        </div>
      </div>
    </header>

    <div ref="listEl" class="message-list" data-od-id="message-list">
      <ChatMessage
        v-if="greetingMessage"
        :key="'greeting'"
        :message="greetingMessage"
        avatar="feixingxuerong.png"
      />
      <ChatMessage
        v-for="(m, index) in chat.messages"
        :key="index"
        :data-od-id="`message-${index}`"
        :message="m"
        :avatar="m.role === 'assistant' ? 'feixingxuerong.png' : 'piaobozhe.jpg'"
      />
    </div>

    <ChatInput />
  </main>
</template>
