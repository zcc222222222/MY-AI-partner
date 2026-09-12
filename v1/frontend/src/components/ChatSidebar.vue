<script setup lang="ts">
import { useChatStore } from '../stores/chat'

const chat = useChatStore()
</script>

<template>
  <aside class="sidebar" data-od-id="chat-sidebar">
    <div class="brand">
      <div class="brand-mark">爱</div>
      <div class="brand-text">
        <strong>{{ chat.activeCharacter?.display_name ?? '飞行雪绒' }}</strong>
        <span>{{ chat.activeCharacter?.name ?? '爱弥斯' }} · AI 陪伴</span>
      </div>
    </div>

    <button class="new-session" data-od-id="new-session-button" @click="chat.newSession()">
      <span aria-hidden="true">+</span> 新建会话
    </button>

    <nav class="session-list" aria-label="会话列表">
      <div
        v-for="s in chat.sessions"
        :key="s.id"
        class="session-row"
        :class="{ active: s.id === chat.activeSessionId }"
      >
        <button
          class="session-title"
          :data-od-id="`session-item-${s.id}`"
          @click="chat.openSession(s.id)"
        >
          {{ s.title }}
        </button>
        <button
          class="session-delete"
          :title="`删除会话:${s.title}`"
          aria-label="删除会话"
          @click="chat.removeSession(s.id)"
        >
          ×
        </button>
      </div>
    </nav>
  </aside>
</template>
