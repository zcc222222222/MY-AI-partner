<script setup lang="ts">
import { ref } from 'vue'
import { useChatStore } from '../stores/chat'

const chat = useChatStore()
const portraitFailed = ref(false)

// 运行时拼接路径,不要写成静态 src="/assets/...":
// @vitejs/plugin-vue 会把模板里的根绝对路径改写成 import,构建期会去文件系统根解析而失败。
// 立绘不存在时,@error 会把面板切到占位提示。
const portraitSrc = '/assets/amis-portrait.jpg'
</script>

<template>
  <aside class="character-panel" data-od-id="character-panel">
    <div class="portrait">
      <img
        v-if="!portraitFailed"
        :src="portraitSrc"
        width="1080"
        height="1701"
        alt="飞行雪绒(爱弥斯)立绘"
        @error="portraitFailed = true"
      />
      <div v-else class="portrait-placeholder">
        <span>飞行雪绒立绘</span>
        <small>将立绘放入 frontend/public/assets/amis-portrait.jpg</small>
      </div>
    </div>
    <div class="character-meta">
      <h2>{{ chat.activeCharacter?.display_name ?? '飞行雪绒' }}</h2>
      <p>{{ chat.activeCharacter?.name ?? '爱弥斯' }} · 角色卡 v{{ chat.activeCharacter?.version ?? '1.0.0' }}</p>
    </div>
  </aside>
</template>
