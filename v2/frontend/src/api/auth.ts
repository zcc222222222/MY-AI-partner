import { apiGet, apiPost } from './client'

export interface User { id: number; username: string; display_name: string; can_manage_knowledge?: boolean }
export interface AuthResult { user: User; csrf_token: string }
export const me = () => apiGet<AuthResult>('/api/v1/auth/me')
export const login = (username: string, password: string) => apiPost<AuthResult>('/api/v1/auth/login', { username, password })
export interface Registration { username: string; password: string; confirm_password: string; email: string; email_code: string; display_name?: string }
export const register = (body: Registration) => apiPost<AuthResult>('/api/v1/auth/register', body)
export const requestEmailCode = (email: string) => apiPost<{ message: string; retry_after: number; expires_in: number }>('/api/v1/auth/email-code', { email })
export const logout = () => apiPost<{ ok: boolean }>('/api/v1/auth/logout')
