<script setup lang="ts">
import { ref } from 'vue'
import { useChatStore } from '../stores/chat'

const chat = useChatStore()
const text = ref('')

async function submit() {
  const value = text.value.trim()
  if (!value || chat.streaming) return
  text.value = ''
  await chat.send(value)
}
</script>

<template>
  <div class="chat-input-wrap" data-od-id="chat-input">
    <div class="chat-input">
      <textarea
        v-model="text"
        rows="1"
        placeholder="和爱弥斯说点什么…"
        @keydown.enter.exact.prevent="submit"
      ></textarea>
      <button
        class="send"
        data-od-id="send-button"
        :disabled="chat.streaming || !text.trim()"
        @click="submit"
      >
        {{ chat.streaming ? '生成中…' : '发送' }}
      </button>
    </div>
    <p v-if="chat.error" class="input-error">{{ chat.error }}</p>
  </div>
</template>
