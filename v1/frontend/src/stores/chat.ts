import { defineStore } from 'pinia'
import { computed, reactive, ref } from 'vue'
import * as api from '../api/chat'
import type { Character, Message, Session } from '../types'

export const useChatStore = defineStore('chat', () => {
  const characters = ref<Character[]>([])
  const sessions = ref<Session[]>([])
  const activeSessionId = ref<string | null>(null)
  const messages = ref<Message[]>([])
  const streaming = ref(false)
  const streamBuffer = ref('')
  const error = ref('')

  const activeCharacter = computed(() => characters.value[0] ?? null)
  /** 已发出请求但还没有任何字到达 —— 也就是模型正在思考(含推理阶段与工具轮次)。 */
  const awaitingFirstToken = computed(() => streaming.value && streamBuffer.value === '')

  async function init() {
    error.value = ''
    characters.value = await api.listCharacters()
    sessions.value = await api.listSessions()
    if (sessions.value.length > 0) await openSession(sessions.value[0].id)
  }

  async function openSession(id: string) {
    activeSessionId.value = id
    streaming.value = false
    streamBuffer.value = ''
    messages.value = await api.listMessages(id)
  }

  async function newSession() {
    const session = await api.createSession()
    sessions.value.unshift(session)
    await openSession(session.id)
  }

  async function removeSession(id: string) {
    await api.deleteSession(id)
    sessions.value = sessions.value.filter((s) => s.id !== id)
    if (activeSessionId.value === id) {
      if (sessions.value.length > 0) await openSession(sessions.value[0].id)
      else {
        activeSessionId.value = null
        messages.value = []
      }
    }
  }

  async function send(content: string) {
    if (streaming.value) return
    const text = content.trim()
    if (!text) return
    if (!activeSessionId.value) await newSession()

    error.value = ''
    const sessionId = activeSessionId.value as string
    messages.value.push({ id: 0, role: 'user', content: text, created_at: new Date().toISOString() })
    // 必须是 reactive 对象:v-for 渲染的是数组里的响应式代理,
    // 直接改这个「裸对象」绕过了代理的 setter,Vue 收不到通知 —— 表现为流式内容要刷新才出现。
    const draft = reactive<Message>({
      id: 0,
      role: 'assistant',
      content: '',
      created_at: new Date().toISOString(),
    })
    messages.value.push(draft)
    streaming.value = true
    streamBuffer.value = ''

    try {
      const messageId = await api.sendMessage(sessionId, text, {
        onDelta: (delta) => {
          draft.content += delta
          streamBuffer.value = draft.content
        },
        onDone: (id) => {
          // 回答已经完整了 —— 立刻解锁,不等后面那次生成标题的请求。
          draft.id = id
          streaming.value = false
          streamBuffer.value = ''
        },
        onTitle: (title) => {
          const target = sessions.value.find((s) => s.id === sessionId)
          if (target) target.title = title
        },
      })
      if (messageId !== null) draft.id = messageId
      sessions.value = await api.listSessions()
    } catch (e) {
      error.value = e instanceof Error ? e.message : '发送失败'
      if (!draft.content) messages.value.pop()
    } finally {
      streaming.value = false
      streamBuffer.value = ''
    }
  }

  return {
    characters,
    activeCharacter,
    awaitingFirstToken,
    sessions,
    activeSessionId,
    messages,
    streaming,
    streamBuffer,
    error,
    init,
    openSession,
    newSession,
    removeSession,
    send,
  }
})
