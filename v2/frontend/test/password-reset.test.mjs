import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { test } from 'node:test'
import { JSDOM } from 'jsdom'
import { parse, compileScript } from '@vue/compiler-sfc'
import ts from 'typescript'

// Import Vue only after installing the DOM; runtime-dom captures document once.
const dom = new JSDOM('<!doctype html><html><body><div id="app"></div></body></html>', { url: 'http://localhost/' })
for (const key of ['window', 'document', 'Document', 'Element', 'HTMLElement', 'SVGElement', 'Node']) globalThis[key] = dom.window[key]
const { createApp, nextTick, reactive } = await import('vue')
const source = await readFile(new URL('../src/components/AuthPanel.vue', import.meta.url), 'utf8')
const { descriptor } = parse(source)
let script = compileScript(descriptor, { id: 'reset-test', inlineTemplate: true }).content
script = script.replaceAll(/from ["']vue["']/g, 'from ' + JSON.stringify(import.meta.resolve('vue')))
  .replace("import { useAuthStore } from '../stores/auth'", 'const useAuthStore = () => globalThis.__resetAuth')
  .replace("import { requestEmailCode, requestPasswordResetCode, resetPassword } from '../api/auth'", 'const { requestEmailCode, requestPasswordResetCode, resetPassword } = globalThis.__resetApi')
const compiled = ts.transpileModule(script, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext } }).outputText
const moduleUrl = 'data:text/javascript;base64,' + Buffer.from(compiled).toString('base64')

test('forgot-password UI submits recovery, returns to login, and never signs in automatically', async () => {
  const calls = []
  globalThis.__resetAuth = reactive({ busy: false, error: '',
    signIn: () => { throw Error('Recovery must not sign in') },
    passwordResetComplete: () => calls.push(['complete']),
  })
  globalThis.__resetApi = {
    requestEmailCode: () => { throw Error('Must use recovery email endpoint') },
    requestPasswordResetCode: async (email) => { calls.push(['code', email]); return { message: 'ack', retry_after: 60 } },
    resetPassword: async (...args) => { calls.push(['reset', ...args]); return { message: '密码已重置，请重新登录' } },
  }
  const { default: component } = await import(moduleUrl)
  const app = createApp(component); app.mount('#app')
  const button = (text) => [...document.querySelectorAll('button')].find((b) => b.textContent.includes(text))
  const set = async (name, value) => {
    const field = document.querySelector(`[name="${name}"]`)
    assert.ok(field); field.value = value; field.dispatchEvent(new window.Event('input', { bubbles: true })); await nextTick()
  }
  try {
    button('忘记密码').click(); await nextTick()
    assert.equal(document.querySelector('h1').textContent, '重置密码')
    assert.equal(document.querySelector('[name=username]'), null)
    await set('email', 'alice@example.com')
    button('获取验证码').click(); await new Promise((r) => setImmediate(r)); await nextTick()
    assert.deepEqual(calls[0], ['code', 'alice@example.com'])
    assert.ok(button('秒后重发').disabled)
    await set('email_code', '123456'); await set('password', 'New-password-456'); await set('confirm_password', 'New-password-456')
    document.querySelector('form').dispatchEvent(new window.Event('submit', { bubbles: true, cancelable: true }))
    await new Promise((r) => setImmediate(r)); await nextTick()
    assert.deepEqual(calls[1], ['reset', 'alice@example.com', '123456', 'New-password-456', 'New-password-456'])
    assert.deepEqual(calls[2], ['complete'])
    assert.equal(document.querySelector('h1').textContent, '欢迎回来')
    assert.equal(document.querySelector('[name=password]').value, '')
    assert.match(document.body.textContent, /密码已重置，请重新登录/)
  } finally {
    app.unmount(); delete globalThis.__resetAuth; delete globalThis.__resetApi
  }
})
