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
  const toolStatus = ref('')
  const error = ref('')
  const creatingSession = ref(false)
  let pendingCreate: Promise<void> | null = null
  const canNewSession = computed(() => !creatingSession.value && !streaming.value &&
    (!activeSessionId.value || sessions.value.find((s) => s.id === activeSessionId.value)?.has_user_message === true))

  // 在途请求。controller 负责真正掐断连接,requestSeq 负责让所有旧回调失效 ——
  // 两者缺一不可:只 abort 的话,已经排进队列的回调仍可能把 streaming 改回 false。
  let controller: AbortController | null = null
  let requestSeq = 0
  let accountEpoch = 0
  let loadSeq = 0

  function reset() {
    accountEpoch++
    loadSeq++
    stop()
    characters.value = []
    sessions.value = []
    messages.value = []
    activeSessionId.value = null
    error.value = ''
    creatingSession.value = false
    pendingCreate = null
  }

  const activeCharacter = computed(() => characters.value[0] ?? null)
  /** 已发出请求但还没有任何字到达 —— 也就是模型正在思考(含推理阶段与工具轮次)。 */
  const awaitingFirstToken = computed(() => streaming.value && streamBuffer.value === '')

  /** 作废在途请求。切会话、删会话、点「停止」、组件卸载都走这里。 */
  function stop() {
    toolStatus.value = ''
    controller?.abort()
    controller = null
    requestSeq += 1
    streaming.value = false
    streamBuffer.value = ''
    // 一个字都没到的空气泡直接抹掉;已经有内容就留给用户看。
    // (注意:被中断的回复不会进库,刷新后会消失 —— v1 接受这个取舍。)
    const last = messages.value[messages.value.length - 1]
    if (last && last.role === 'assistant' && !last.content) messages.value.pop()
  }

  async function init() {
    const epoch = accountEpoch
    error.value = ''
    const [loadedCharacters, loadedSessions] = await Promise.all([api.listCharacters(), api.listSessions()])
    if (epoch !== accountEpoch) return
    characters.value = loadedCharacters
    sessions.value = loadedSessions
    if (sessions.value.length > 0) await openSession(sessions.value[0].id)
  }

  async function openSession(id: string) {
    const epoch = accountEpoch
    const seq = ++loadSeq
    // 切会话前先在途请求作废:否则 A 的流会在 B 的界面上把状态改掉。
    stop()
    activeSessionId.value = id
    const loaded = await api.listMessages(id)
    if (epoch === accountEpoch && seq === loadSeq) messages.value = loaded
  }

  async function newSession() {
    if (pendingCreate) return pendingCreate
    if (!canNewSession.value) return
    const epoch = accountEpoch
    creatingSession.value = true
    const task = (async () => {
      try {
        const session = await api.createSession()
        if (epoch !== accountEpoch) return
        sessions.value = [session, ...sessions.value.filter((s) => s.id !== session.id)]
        await openSession(session.id)
      } catch (e) {
        if (epoch === accountEpoch) error.value = e instanceof Error ? e.message : '创建会话失败'
      } finally { if (epoch === accountEpoch) creatingSession.value = false }
    })()
    pendingCreate = task
    try { await task } finally { if (pendingCreate === task) pendingCreate = null }
  }

  async function removeSession(id: string) {
    const epoch = accountEpoch
    if (activeSessionId.value === id) stop()
    await api.deleteSession(id)
    if (epoch !== accountEpoch) return
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
    const epoch = accountEpoch
    if (streaming.value) return
    const text = content.trim()
    if (!text) return
    if (!activeSessionId.value) await newSession()
    if (epoch !== accountEpoch || !activeSessionId.value) return

    error.value = ''
    const sessionId = activeSessionId.value as string
    const seq = ++requestSeq
    const ac = new AbortController()
    controller = ac

    messages.value.push({ id: 0, role: 'user', content: text, created_at: new Date().toISOString() })
    const targetSession = sessions.value.find((s) => s.id === sessionId)
    if (targetSession) targetSession.has_user_message = true
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
    toolStatus.value = ''
    streamBuffer.value = ''

    try {
      const messageId = await api.sendMessage(
        sessionId,
        text,
        {
          onStatus: (message) => { if (seq === requestSeq) toolStatus.value = message },
          onDelta: (delta) => {
            if (seq !== requestSeq) return
            draft.content += delta
            streamBuffer.value = draft.content
          },
          onDone: (id) => {
            if (seq !== requestSeq) return
            // 回答已经完整了 —— 立刻解锁,不等后面那次生成标题的请求。
            draft.id = id
            toolStatus.value = ''
            streaming.value = false
            streamBuffer.value = ''
          },
          onReplace: (text) => {
            if (seq !== requestSeq) return
            draft.content = text
            streamBuffer.value = text
          },
          onTitle: (title) => {
            if (seq !== requestSeq) return
            const target = sessions.value.find((s) => s.id === sessionId)
            if (target) target.title = title
          },
        },
        ac.signal,
      )
      if (seq !== requestSeq) return
      if (messageId !== null) draft.id = messageId
      const loaded = await api.listSessions()
      if (seq === requestSeq) sessions.value = loaded
    } catch (e) {
      if (seq !== requestSeq) return
      // 用户主动停止:不是错误,不要弹提示。
      if (e instanceof DOMException && e.name === 'AbortError') return
      error.value = e instanceof Error ? e.message : '发送失败'
      if (!draft.content) messages.value.pop()
    } finally {
      if (seq === requestSeq) {
        toolStatus.value = ''
        streaming.value = false
        streamBuffer.value = ''
        controller = null
      }
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
    toolStatus,
    error,
    creatingSession,
    canNewSession,
    init,
    openSession,
    newSession,
    removeSession,
    send,
    stop,
    reset,
  }
})
