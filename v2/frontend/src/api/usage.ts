import { apiGet, apiPost } from './client'

export interface Bucket {
  questions: number; spent_micros: number; reserved_micros: number; remaining_micros: number; budget_micros: number
}
export interface Allowance {
  remaining_questions: number; paused: boolean; user: Bucket
  limits: { user_daily_questions: number; global_daily_questions: number; price_version: string }
}
export interface UsageOverview extends Allowance {
  global_paused: boolean
  global_day: Bucket; global_month: Bucket
  users: { owner_id: number; username: string; questions: number; spent_micros: number; reserved_micros: number; paused: boolean }[]
  requests: { id: string; owner_id: number; status: string; charged_micros: number; unknown_calls: number; created_at: string }[]
}
export const allowance = () => apiGet<Allowance>('/api/v1/chat/quota')
export const overview = () => apiGet<UsageOverview>('/api/v1/usage/admin')
export const pause = (paused: boolean, owner_id?: number) => apiPost('/api/v1/usage/admin/pause', { paused, ...(owner_id ? { owner_id } : {}) })
