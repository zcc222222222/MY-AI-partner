import { apiDelete, apiGet, apiPost, streamPost } from './client'
import type { Character, Message, Session } from '../types'

export const listCharacters = () => apiGet<Character[]>('/api/v1/characters')
export const listSessions = () => apiGet<Session[]>('/api/v1/chat/sessions')
export const createSession = () => apiPost<Session>('/api/v1/chat/sessions', {})
export const deleteSession = (id: string) => apiDelete(`/api/v1/chat/sessions/${id}`)
export const listMessages = (id: string) =>
  apiGet<Message[]>(`/api/v1/chat/sessions/${id}/messages`)

/** 发送消息并流式接收助手回复,返回持久化后的消息 id。 */
export async function sendMessage(
  sessionId: string,
  content: string,
  onDelta: (text: string) => void,
  signal?: AbortSignal,
): Promise<number | null> {
  let messageId: number | null = null
  let streamError: string | null = null
  await streamPost(
    `/api/v1/chat/sessions/${sessionId}/messages`,
    { content },
    (event) => {
      if (event.type === 'delta' && typeof event.content === 'string') onDelta(event.content)
      else if (event.type === 'done' && typeof event.message_id === 'number')
        messageId = event.message_id
      else if (event.type === 'error') streamError = String(event.detail ?? '生成失败')
    },
    signal,
  )
  if (streamError) throw new Error(streamError)
  return messageId
}
