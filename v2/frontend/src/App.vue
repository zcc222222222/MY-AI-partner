<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useChatStore } from './stores/chat'
import { useBackgroundStore } from './stores/background'
import ChatSidebar from './components/ChatSidebar.vue'
import ChatView from './components/ChatView.vue'
import CharacterPanel from './components/CharacterPanel.vue'
import AuthPanel from './components/AuthPanel.vue'
import { useAuthStore } from './stores/auth'
import KnowledgePanel from './components/KnowledgePanel.vue'

const BACKGROUND_SRC = '/assets/app-background.mp4'
const BACKGROUND_POSTER = '/assets/app-background.webp'

const chat = useChatStore()
const background = useBackgroundStore()
const auth = useAuthStore()
const knowledgeOpen = ref(false)
let authChannel: BroadcastChannel | null = null
const videoRef = ref<HTMLVideoElement | null>(null)

onMounted(() => {
  background.attach(videoRef.value)
  background.init()
  window.addEventListener('auth-expired', onExpired)
  if (typeof BroadcastChannel !== 'undefined') {
    authChannel = new BroadcastChannel('aipartner-auth')
    authChannel.onmessage = () => { auth.expire(); chat.reset(); void auth.restore() }
  }
  void auth.restore()
})

function onExpired() { auth.expire(); chat.reset() }
watch(() => auth.user?.id, (id) => {
  knowledgeOpen.value = false
  chat.reset()
  if (id) chat.init().catch((e: unknown) => { chat.error = e instanceof Error ? e.message : '初始化失败' })
})

onBeforeUnmount(() => {
  // 卸载时掐断在途流,避免 HMR / 路由切换留下悬挂请求继续改状态。
  chat.stop()
  background.dispose()
  window.removeEventListener('auth-expired', onExpired)
  authChannel?.close()
})
</script>

<template>
  <!-- 全页视频背景。不写 autoplay:由 store 统一决定播/停,免得和
       「减少动态效果」「标签页隐藏自动暂停」打架。静音 + playsinline 才能免交互播放。 -->
  <video
    v-show="!background.unavailable"
    ref="videoRef"
    class="bg-video"
    :src="BACKGROUND_SRC"
    :poster="BACKGROUND_POSTER"
    muted
    loop
    playsinline
    preload="auto"
    aria-hidden="true"
    tabindex="-1"
    @error="background.onError"
  ></video>

  <div v-if="!auth.ready" class="auth-shell" role="status">正在确认登录状态…</div>
  <AuthPanel v-else-if="!auth.user" />
  <div v-else class="app-shell">
    <ChatSidebar @manage-knowledge="knowledgeOpen = true" />
    <ChatView />
    <CharacterPanel />
    <KnowledgePanel v-if="knowledgeOpen && auth.user.can_manage_knowledge" :key="auth.user.id" @close="knowledgeOpen = false" />
  </div>
</template>
