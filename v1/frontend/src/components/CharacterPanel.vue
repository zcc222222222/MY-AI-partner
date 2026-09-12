<script setup lang="ts">
import { ref } from 'vue'
import { useChatStore } from '../stores/chat'

const chat = useChatStore()
const portraitFailed = ref(false)
</script>

<template>
  <aside class="character-panel" data-od-id="character-panel">
    <div class="portrait">
      <img
        v-if="!portraitFailed"
        src="/assets/amis-portrait.png"
        alt="爱弥斯立绘"
        @error="portraitFailed = true"
      />
      <div v-else class="portrait-placeholder">
        <span>爱弥斯立绘</span>
        <small>将透明立绘放入 frontend/public/assets/amis-portrait.png</small>
      </div>
    </div>
    <div class="character-meta">
      <h2>{{ chat.activeCharacter?.display_name ?? '飞行雪绒' }}</h2>
      <p>{{ chat.activeCharacter?.name ?? '爱弥斯' }} · 角色卡 v{{ chat.activeCharacter?.version ?? '1.0.0' }}</p>
    </div>
  </aside>
</template>
