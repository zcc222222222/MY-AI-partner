import { apiDelete, apiGet, apiPost, streamPost, authExpired } from './client'
import type { Character, Message, Session } from '../types'

export const listCharacters = () => apiGet<Character[]>('/api/v1/characters')
export const listSessions = () => apiGet<Session[]>('/api/v1/chat/sessions')
export const createSession = () => apiPost<Session>('/api/v1/chat/sessions', {})
export const deleteSession = (id: string) => apiDelete(`/api/v1/chat/sessions/${id}`)
export const listMessages = (id: string) =>
  apiGet<Message[]>(`/api/v1/chat/sessions/${id}/messages`)

export interface StreamHandlers {
  onDelta: (text: string) => void
  onReplace?: (text: string) => void
  /** 回答已完整到达。标题生成还在后面,别在这里等它。 */
  onDone?: (messageId: number) => void
  /** 首条消息的会话标题生成完毕,单独补发。 */
  onTitle?: (title: string) => void
  onStatus?: (message: string) => void
}

/** 发送消息并流式接收助手回复,返回持久化后的消息 id。 */
export async function sendMessage(
  sessionId: string,
  content: string,
  handlers: StreamHandlers,
  signal?: AbortSignal,
  requestId: string = crypto.randomUUID(),
): Promise<number | null> {
  let messageId: number | null = null
  let streamError: string | null = null
  await streamPost(
    `/api/v1/chat/sessions/${sessionId}/messages`,
    { content, client_request_id: requestId },
    (event) => {
      if (event.type === 'delta' && typeof event.content === 'string') {
        handlers.onDelta(event.content)
      } else if (event.type === 'tool_status' && typeof event.message === 'string') {
        handlers.onStatus?.(event.message)
      } else if (event.type === 'replace' && typeof event.content === 'string') {
        handlers.onReplace?.(event.content)
      } else if (event.type === 'done' && typeof event.message_id === 'number') {
        messageId = event.message_id
        handlers.onDone?.(event.message_id)
      } else if (event.type === 'title' && typeof event.title === 'string') {
        handlers.onTitle?.(event.title)
      } else if (event.type === 'error') {
        if (event.status_code === 401) authExpired()
        streamError = String(event.detail ?? '生成失败')
      }
    },
    signal,
  )
  if (streamError) throw new Error(streamError)
  if (messageId === null) throw new Error('连接已结束，但未收到完整回答，请重试。')
  return messageId
}
