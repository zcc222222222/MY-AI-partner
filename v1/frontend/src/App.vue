<script setup lang="ts">
import { onMounted } from 'vue'
import { useChatStore } from './stores/chat'
import ChatSidebar from './components/ChatSidebar.vue'
import ChatView from './components/ChatView.vue'
import CharacterPanel from './components/CharacterPanel.vue'

const chat = useChatStore()

onMounted(() => {
  chat.init().catch((e: unknown) => {
    chat.error = e instanceof Error ? e.message : '初始化失败'
  })
})
</script>

<template>
  <div class="app-shell">
    <ChatSidebar />
    <ChatView />
    <CharacterPanel />
  </div>
</template>
