import { apiGet, apiPost } from './client'

export interface User { id: number; username: string; display_name: string; can_manage_knowledge?: boolean; is_guest?: boolean }
export interface AuthResult { user: User; csrf_token: string }
export const me = () => apiGet<AuthResult>('/api/v1/auth/me')
export const login = (username: string, password: string) => apiPost<AuthResult>('/api/v1/auth/login', { username, password })
export const enterGuest = () => apiPost<AuthResult>('/api/v1/auth/guest', {})
export interface Registration { username: string; password: string; confirm_password: string; email: string; email_code: string; display_name?: string }
export const register = (body: Registration) => apiPost<AuthResult>('/api/v1/auth/register', body)
export const requestEmailCode = (email: string) => apiPost<{ message: string; retry_after: number; expires_in: number }>('/api/v1/auth/email-code', { email })
export const requestPasswordResetCode = (email: string) => apiPost<{ message: string; retry_after: number; expires_in: number }>('/api/v1/auth/password-reset/code', { email })
export const resetPassword = (email: string, email_code: string, new_password: string, confirm_password: string) =>
  apiPost<{ message: string }>('/api/v1/auth/password-reset', { email, email_code, new_password, confirm_password })
export const logout = () => apiPost<{ ok: boolean }>('/api/v1/auth/logout')
