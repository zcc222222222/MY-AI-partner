<script setup lang="ts">
import { computed } from 'vue'
import type { Message } from '../types'
import { renderMarkdown, handleImageError, handleImageOpen } from '../markdown'

const props = defineProps<{
  message: Message
  avatar: string
  /** 模型尚未吐出第一个字时,气泡里显示「正在思考中」而不是空白。 */
  thinking?: boolean
  thinkingLabel?: string
}>()

const avatarUrl = (name: string) => `/assets/${name}`

/**
 * 只有助手回复走 Markdown。用户消息保持纯文本:
 * 让用户随手打的 `*` 或缩进变成斜体/代码块只会让人困惑,
 * 而模型输出确实会带表格、列表、代码块。
 */
const isAssistant = computed(() => props.message.role === 'assistant')

const html = computed(() =>
  isAssistant.value ? renderMarkdown(props.message.content) : '',
)
</script>

<template>
  <div class="message" :class="message.role">
    <img
      class="avatar"
      :src="avatarUrl(avatar)"
      :alt="message.role === 'assistant' ? '爱弥斯' : '漂泊者'"
    />
    <div class="bubble" :class="{ 'bubble-md': isAssistant && !thinking }">
      <span v-if="thinking" class="thinking" role="status">
        {{ thinkingLabel || '飞行雪绒正在思考中' }}<span class="thinking-dots" aria-hidden="true"><span></span><span></span><span></span></span>
      </span>
      <!-- 内容已由 markdown.ts 白名单清洗过;见该文件顶部的安全说明 -->
      <div v-else-if="isAssistant" class="md" v-html="html" @error.capture="handleImageError" @click="handleImageOpen" @keydown.enter="handleImageOpen" @keydown.space="handleImageOpen"></div>
      <template v-else>{{ message.content }}</template>
    </div>
  </div>
</template>
