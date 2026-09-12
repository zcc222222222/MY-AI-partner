<script setup lang="ts">
import type { Message } from '../types'

defineProps<{
  message: Message
  avatar: string
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
    <div class="bubble">{{ message.content }}</div>
  </div>
</template>
