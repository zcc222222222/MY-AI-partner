import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
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
    const draft: Message = { id: 0, role: 'assistant', content: '', created_at: new Date().toISOString() }
    messages.value.push(draft)
    streaming.value = true
    streamBuffer.value = ''

    try {
      const messageId = await api.sendMessage(sessionId, text, (delta) => {
        draft.content += delta
        streamBuffer.value = draft.content
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
