<script setup lang="ts">
import type { Message } from '../types'

defineProps<{
  message: Message
  avatar: string
  /** 模型尚未吐出第一个字时,气泡里显示「正在思考中」而不是空白。 */
  thinking?: boolean
}>()

const avatarUrl = (name: string) => `/assets/${name}`
</script>

<template>
  <div class="message" :class="message.role">
    <img
      class="avatar"
      :src="avatarUrl(avatar)"
      :alt="message.role === 'assistant' ? '爱弥斯' : '漂泊者'"
    />
    <div class="bubble">
      <span v-if="thinking" class="thinking" role="status">
        飞行雪绒正在思考中<span class="thinking-dots" aria-hidden="true"><span></span><span></span><span></span></span>
      </span>
      <template v-else>{{ message.content }}</template>
    </div>
  </div>
</template>
