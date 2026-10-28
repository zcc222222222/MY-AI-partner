import { apiGet, apiPost } from './client'

export interface KnowledgeJob {
  id: string; recipe: string; state: string; phase: string; created_by: number
  created_at: string; updated_at: string; finished_at?: string; attempt: number
  error?: string; can_resume: boolean
  progress?: { site?: string; name?: string; page_status?: string }
  report?: { summary?: Record<string, number>; impact_reviews?: Record<string, number>
    collection?: { site: string; status: string; imported: number; finished_at?: string }[]
    issues?: { label: string; status: string; retryable?: boolean; retry_at?: string }[] }
}
export interface KnowledgeOverview {
  checked_at: string
  counts: { sources: number; pages: number; dirty_pages: number; pending_pages: number }
  sources: { site: string; last_revision_at?: string; checked_at?: string; last_status?: string; freshness: string; check_after_days: number }[]
  periods: { topic: string; status: string; start?: string; end?: string; source_name?: string }[]
  pending_pages: { page_id: string; title: string; status: string }[]
  planning_error_count: number; impact_reviews: Record<string, number>; jobs: KnowledgeJob[]
  live_guides: { status: string; last_attempt_at?: string; last_success_at?: string; wait_seconds: number; cache_entries: number }
  retrievals: RetrievalRecord[]
  recipes: { id: string; title: string; description: string; live: boolean }[]
}
const ROOT = '/api/v1/knowledge/ops'
export interface RetrievalRecord {
  id: string; tool: string; topic?: string; origin: string; state: string; started_at: string; finished_at?: string
  elapsed_ms?: number; result_count?: number; image_count?: number; cache: string; stage?: string; reason?: string
  retry_after_seconds?: number; can_retry: boolean
}
export const overview = () => apiGet<KnowledgeOverview>(ROOT)
export const job = (id: string) => apiGet<KnowledgeJob>(`${ROOT}/jobs/${encodeURIComponent(id)}`)
export const start = (recipe: string) => apiPost<KnowledgeJob>(`${ROOT}/jobs`, { recipe })
export const resume = (id: string) => apiPost<KnowledgeJob>(`${ROOT}/jobs/${encodeURIComponent(id)}/resume`)
export const retrievals = () => apiGet<{records: RetrievalRecord[]}>(`${ROOT}/retrievals`)
export const recheck = (topic: string) => apiPost<RetrievalRecord>(`${ROOT}/retrievals/recheck`,{topic})
