<script setup lang="ts">
import { computed, ref } from 'vue'
import { useChatStore } from '../stores/chat'
import { useBackgroundStore } from '../stores/background'

// 必须与后端 schemas.MessageCreate 的 max_length 一致(改一处要同步另一处)。
const MAX_CHARS = 500
// 只在接近上限时才显示计数:常驻的「0 / 500」是噪音。
const COUNTER_FROM = 400

const chat = useChatStore()
const background = useBackgroundStore()
const text = ref('')

const pauseLabel = computed(() => (background.paused ? '播放背景动画' : '暂停背景动画'))
const soundLabel = computed(() => (background.soundOn ? '关闭背景音' : '打开背景音'))
const showCounter = computed(() => text.value.length >= COUNTER_FROM)

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
        :maxlength="MAX_CHARS"
        placeholder="和爱弥斯说点什么…"
        @keydown.enter.exact.prevent="submit"
      ></textarea>

      <!-- 背景控制:两个 ghost 次级按钮,不跟主操作抢视觉重量。
           图标本身就是状态:播放中显示暂停键,有声时显示带声波的喇叭。 -->
      <button
        class="bg-toggle"
        data-od-id="bg-toggle"
        type="button"
        :title="pauseLabel"
        :aria-label="pauseLabel"
        @click="background.toggle()"
      >
        <svg v-if="background.paused" viewBox="0 0 24 24" aria-hidden="true">
          <path d="M9 6.4 17.6 12 9 17.6Z" fill="currentColor" />
        </svg>
        <svg v-else viewBox="0 0 24 24" aria-hidden="true">
          <rect x="7" y="6" width="3.6" height="12" rx="1.3" fill="currentColor" />
          <rect x="13.4" y="6" width="3.6" height="12" rx="1.3" fill="currentColor" />
        </svg>
      </button>

      <button
        class="bg-toggle"
        data-od-id="bg-sound-toggle"
        type="button"
        :title="soundLabel"
        :aria-label="soundLabel"
        @click="background.toggleSound()"
      >
        <svg v-if="background.soundOn" viewBox="0 0 24 24" aria-hidden="true">
          <path d="M4.5 9.2h3.1L11.8 5.6v12.8L7.6 14.8H4.5z" fill="currentColor" />
          <path
            d="M15.2 9.4a3.6 3.6 0 0 1 0 5.2"
            fill="none"
            stroke="currentColor"
            stroke-width="1.7"
            stroke-linecap="round"
          />
          <path
            d="M17.7 6.9a7 7 0 0 1 0 10.2"
            fill="none"
            stroke="currentColor"
            stroke-width="1.7"
            stroke-linecap="round"
          />
        </svg>
        <svg v-else viewBox="0 0 24 24" aria-hidden="true">
          <path d="M4.5 9.2h3.1L11.8 5.6v12.8L7.6 14.8H4.5z" fill="currentColor" />
          <path
            d="M15.4 9.9 19.8 14.3M19.8 9.9 15.4 14.3"
            fill="none"
            stroke="currentColor"
            stroke-width="1.7"
            stroke-linecap="round"
          />
        </svg>
      </button>

      <!-- 主操作位:生成中变成「停止」,两者互斥,同一位置只有一个实心按钮。 -->
      <button
        v-if="chat.streaming"
        class="send"
        data-od-id="stop-button"
        type="button"
        @click="chat.stop()"
      >
        停止
      </button>
      <button
        v-else
        class="send"
        data-od-id="send-button"
        type="button"
        :disabled="!text.trim()"
        @click="submit"
      >
        发送
      </button>
    </div>

    <p v-if="showCounter" class="input-counter" data-od-id="input-counter">
      {{ text.length }} / {{ MAX_CHARS }}
    </p>
    <p v-if="chat.error" class="input-error">{{ chat.error }}</p>
  </div>
</template>
