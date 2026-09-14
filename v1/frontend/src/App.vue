<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { useChatStore } from './stores/chat'
import { useBackgroundStore } from './stores/background'
import ChatSidebar from './components/ChatSidebar.vue'
import ChatView from './components/ChatView.vue'
import CharacterPanel from './components/CharacterPanel.vue'

const BACKGROUND_SRC = '/assets/app-background.mp4'
const BACKGROUND_POSTER = '/assets/app-background.webp'

const chat = useChatStore()
const background = useBackgroundStore()
const videoRef = ref<HTMLVideoElement | null>(null)

onMounted(() => {
  background.attach(videoRef.value)
  background.init()
  chat.init().catch((e: unknown) => {
    chat.error = e instanceof Error ? e.message : '初始化失败'
  })
})

onBeforeUnmount(() => background.dispose())
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

  <div class="app-shell">
    <ChatSidebar />
    <ChatView />
    <CharacterPanel />
  </div>
</template>
