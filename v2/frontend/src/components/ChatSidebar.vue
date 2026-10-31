<script setup lang="ts">
import { useChatStore } from '../stores/chat'
import { useAuthStore } from '../stores/auth'
import UsageStatus from './UsageStatus.vue'

const chat = useChatStore()
const auth = useAuthStore()
const emit = defineEmits<{ manageKnowledge: [] }>()
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

    <button class="new-session" data-od-id="new-session-button" :disabled="!chat.canNewSession" :title="chat.canNewSession ? '新建会话' : '请先在当前新会话发送一条消息'" @click="chat.newSession()">
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
    <div class="account-menu">
      <UsageStatus v-if="!auth.user?.is_guest" />
      <button v-if="auth.user?.can_manage_knowledge" type="button" @click="emit('manageKnowledge')">知识库管理</button>
      <span>{{ auth.user?.display_name }}</span>
      <button type="button" :disabled="auth.busy" @click="auth.signOut()">{{ auth.user?.is_guest ? '注册 / 登录' : '退出登录' }}</button>
      <p v-if="auth.error" class="auth-error" role="alert">{{ auth.error }}</p>
    </div>
  </aside>
</template>
