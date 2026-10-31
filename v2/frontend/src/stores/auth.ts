import { defineStore } from 'pinia'
import { ref } from 'vue'
import * as api from '../api/auth'
import { setCsrfToken } from '../api/client'

export const useAuthStore = defineStore('auth', () => {
  const user = ref<api.User | null>(null)
  const ready = ref(false)
  const busy = ref(false)
  const error = ref('')
  let epoch = 0
  function expire() {
    epoch++
    user.value = null
    setCsrfToken('')
  }
  function changed() {
    if (typeof BroadcastChannel !== 'undefined') {
      const channel = new BroadcastChannel('aipartner-auth')
      channel.postMessage('changed')
      channel.close()
    }
  }
  async function restore() {
    const turn = ++epoch
    try {
      const result = await api.me()
      if (turn !== epoch) return
      setCsrfToken(result.csrf_token)
      user.value = result.user
    } catch {
      if (turn === epoch) { user.value = null; setCsrfToken('') }
    } finally { ready.value = true }
  }
  async function signIn(username: string, password: string, registration?: Omit<api.Registration, 'username' | 'password'>) {
    const turn = ++epoch
    busy.value = true
    error.value = ''
    try {
      const result = registration ? await api.register({ ...registration, username, password }) : await api.login(username, password)
      if (turn !== epoch) return
      setCsrfToken(result.csrf_token)
      user.value = result.user
      changed()
    } catch (e) {
      error.value = e instanceof Error ? e.message : '登录失败'
    } finally { busy.value = false; ready.value = true }
  }
  async function signOut() {
    busy.value = true
    error.value = ''
    try { await api.logout(); expire(); changed() }
    catch (e) { error.value = e instanceof Error ? e.message : '退出失败，请重试' }
    finally { busy.value = false }
  }
  function passwordResetComplete() { expire(); changed(); ready.value = true }
  return { user, ready, busy, error, restore, signIn, signOut, expire, passwordResetComplete }
})
