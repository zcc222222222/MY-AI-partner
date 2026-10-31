<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useAuthStore } from '../stores/auth'
import { requestEmailCode, requestPasswordResetCode, resetPassword } from '../api/auth'
const auth = useAuthStore()
const mode = ref<'login' | 'register' | 'reset'>('login')
const registering = computed(() => mode.value === 'register')
const resetting = computed(() => mode.value === 'reset')
const newPassword = computed(() => mode.value !== 'login')
const successNotice = ref('')
const username = ref('')
const password = ref('')
const displayName = ref('')
const email = ref('')
const emailCode = ref('')
const confirmPassword = ref('')
const sendingCode = ref(false)
const codeNotice = ref('')
const cooldown = ref(0)
const emailInput = ref<HTMLInputElement | null>(null)
let timer: ReturnType<typeof setInterval> | undefined
let codeEpoch = 0
watch(email, () => { codeEpoch++; codeNotice.value = ''; emailCode.value = ''; cooldown.value = 0; clearInterval(timer) })
onBeforeUnmount(() => { codeEpoch++; clearInterval(timer) })
function switchMode(next: 'login' | 'register' | 'reset') {
  if (auth.busy || sendingCode.value) return
  mode.value = next
  codeEpoch++; clearInterval(timer); cooldown.value = 0
  auth.error = ''; codeNotice.value = ''; successNotice.value = ''
  password.value = ''; confirmPassword.value = ''; emailCode.value = ''
}
async function sendCode() {
  if (sendingCode.value || cooldown.value > 0 || !emailInput.value?.reportValidity()) return
  const turn = ++codeEpoch
  sendingCode.value = true
  auth.error = ''; codeNotice.value = ''
  try {
    const result = await (resetting.value ? requestPasswordResetCode : requestEmailCode)(email.value.trim())
    if (turn !== codeEpoch) return
    codeNotice.value = result.message
    cooldown.value = result.retry_after
    clearInterval(timer)
    timer = setInterval(() => { cooldown.value = Math.max(0, cooldown.value - 1); if (!cooldown.value) clearInterval(timer) }, 1000)
  } catch (e) { if (turn === codeEpoch) auth.error = e instanceof Error ? e.message : '验证码发送失败' }
  finally { sendingCode.value = false }
}
async function submit() {
  if (auth.busy || sendingCode.value) return
  if (newPassword.value && password.value !== confirmPassword.value) { auth.error = '两次输入的密码不一致'; return }
  if (resetting.value) {
    auth.busy = true; auth.error = ''
    try {
      const result = await resetPassword(email.value.trim(), emailCode.value, password.value, confirmPassword.value)
      auth.passwordResetComplete()
      auth.busy = false
      switchMode('login')
      successNotice.value = result.message
    } catch (e) { auth.error = e instanceof Error ? e.message : '密码重置失败，请重试' }
    finally { auth.busy = false; password.value = ''; confirmPassword.value = '' }
    return
  }
  try { await auth.signIn(username.value, password.value, registering.value ? {
    email: email.value.trim(), email_code: emailCode.value, confirm_password: confirmPassword.value,
    display_name: displayName.value.trim() || undefined,
  } : undefined) }
  finally { password.value = ''; confirmPassword.value = '' }
}
</script>

<template>
  <main class="auth-shell">
    <form class="auth-card" @submit.prevent="submit">
      <div class="auth-brand">爱弥斯 · AI 陪伴</div>
      <h1>{{ resetting ? '重置密码' : registering ? '创建你的账号' : '欢迎回来' }}</h1>
      <p class="auth-intro">{{ resetting ? '使用注册时已验证的邮箱接收验证码，设置新密码。' : '登录后继续你的私人对话。' }}</p>
      <p v-if="successNotice" class="auth-hint" role="status">{{ successNotice }}</p>
      <template v-if="mode === 'login'">
        <button class="auth-submit guest-entry" type="button" :disabled="auth.busy" @click="auth.enterGuest()">{{ auth.busy ? '请稍候…' : '免注册体验' }}</button>
        <p class="auth-hint guest-entry-hint">每天可体验3次提问，同一网络共享额度。体验会话24小时后过期。</p>
      </template>
      <label v-if="!resetting">用户名<input v-model="username" name="username" autocomplete="username" required minlength="3" maxlength="32" pattern="[A-Za-z0-9_]+" placeholder="3–32位字母、数字或下划线" :disabled="auth.busy" /></label>
      <p v-if="registering" class="auth-hint">用户名忽略大小写且不能重复；昵称可以重复。</p>
      <label v-if="registering">昵称（选填）<input v-model="displayName" name="displayName" autocomplete="nickname" maxlength="64" :disabled="auth.busy" /></label>
      <template v-if="newPassword">
        <label>邮箱<input ref="emailInput" v-model="email" name="email" type="email" autocomplete="email" required maxlength="254" placeholder="填写可以接收验证码的邮箱" :disabled="auth.busy || sendingCode" /></label>
        <label>邮箱验证码<div class="email-code-row"><input v-model="emailCode" name="email_code" inputmode="numeric" autocomplete="one-time-code" required pattern="[0-9]{6}" maxlength="6" placeholder="6位数字" :disabled="auth.busy" /><button type="button" class="ghost" :disabled="auth.busy || sendingCode || cooldown > 0" @click="sendCode">{{ sendingCode ? '发送中…' : cooldown ? `${cooldown}秒后重发` : '获取验证码' }}</button></div></label>
        <p v-if="codeNotice" class="auth-hint" role="status">{{ codeNotice }}（10分钟内有效）</p>
      </template>
      <label>{{ resetting ? '新密码' : '密码' }}<input v-model="password" name="password" type="password" :autocomplete="newPassword ? 'new-password' : 'current-password'" required :minlength="newPassword ? 10 : 1" maxlength="128" :pattern="newPassword ? '(?=.*[A-Za-z])(?=.*[0-9]).{10,128}' : undefined" :placeholder="newPassword ? '10–128字符，至少包含英文字母和数字' : '输入密码'" :disabled="auth.busy" /></label>
      <p v-if="newPassword" class="auth-hint">至少包含英文字母和数字，可加入符号；首尾不留空格，避免常见弱密码。</p>
      <label v-if="newPassword">确认密码<input v-model="confirmPassword" name="confirm_password" type="password" autocomplete="new-password" required minlength="10" maxlength="128" placeholder="再次输入密码" :disabled="auth.busy" /></label>
      <p v-if="resetting" class="auth-hint">验证码10分钟内有效。重置后所有设备需重新登录，聊天记录保留。</p>
      <p v-if="auth.error" class="auth-error" role="alert">{{ auth.error }}</p>
      <button class="auth-submit" type="submit" :disabled="auth.busy || sendingCode">{{ auth.busy ? '请稍候…' : resetting ? '重置密码' : registering ? '注册并登录' : '登录' }}</button>
      <button v-if="mode === 'login'" class="auth-switch" type="button" :disabled="auth.busy || sendingCode" @click="switchMode('reset')">忘记密码？</button>
      <button class="auth-switch" type="button" :disabled="auth.busy || sendingCode" @click="switchMode(mode === 'login' ? 'register' : 'login')">{{ mode === 'login' ? '还没有账号？注册' : '返回登录' }}</button>
    </form>
  </main>
</template>
