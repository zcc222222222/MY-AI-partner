<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { useChatStore } from '../stores/chat'
import { useAuthStore } from '../stores/auth'
import type { Message } from '../types'
import ChatMessage from './ChatMessage.vue'
import ChatInput from './ChatInput.vue'
import UsageStatus from './UsageStatus.vue'

const chat = useChatStore()
const auth = useAuthStore()
const listEl = ref<HTMLElement | null>(null)
const examples = ['爱弥斯怎么配队？', '爱弥斯的武器和声骸怎么选？', '本期深塔有什么攻略？']

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
          <button class="ghost" :disabled="!chat.canNewSession" title="发送一条消息后可继续新建会话" @click="startNew">新建</button>
          <select
            :value="chat.activeSessionId ?? ''"
            aria-label="切换会话"
            @change="onSessionChange"
          >
            <option value="" disabled>选择会话</option>
            <option v-for="s in chat.sessions" :key="s.id" :value="s.id">{{ s.title }}</option>
          </select>
          <button class="ghost" :title="auth.user?.display_name" :disabled="auth.busy" @click="auth.signOut()">{{ auth.user?.is_guest ? '注册/登录' : '退出' }}</button>
        </div>
      </div>
    </header>
    <p v-if="auth.error" class="mobile-auth-error auth-error" role="alert">{{ auth.error }}</p>
    <div v-if="auth.user?.is_guest" class="guest-banner" role="status">
      <div><span>免注册体验 · 同一网络共享额度。会话24小时后过期，不会自动转入注册账号。</span><UsageStatus /></div>
      <button class="ghost" type="button" :disabled="auth.busy || chat.streaming" @click="auth.signOut()">注册 / 登录</button>
    </div>

    <div ref="listEl" class="message-list" data-od-id="message-list">
      <ChatMessage
        v-if="greetingMessage"
        :key="'greeting'"
        :message="greetingMessage"
        avatar="feixingxuerong.png"
      />
      <div v-if="auth.user?.is_guest && greetingMessage" class="guest-examples" aria-label="体验问题示例">
        <p>可以从这些问题开始：</p>
        <button v-for="question in examples" :key="question" type="button" class="ghost" :disabled="chat.streaming || chat.creatingSession" @click="chat.send(question)">{{ question }}</button>
      </div>
      <ChatMessage
        v-for="(m, index) in chat.messages"
        :key="index"
        :data-od-id="`message-${index}`"
        :message="m"
        :thinking="chat.awaitingFirstToken && index === chat.messages.length - 1"
        :thinking-label="chat.toolStatus"
        :avatar="m.role === 'assistant' ? 'feixingxuerong.png' : 'piaobozhe.jpg'"
      />
    </div>

    <ChatInput />
  </main>
</template>
