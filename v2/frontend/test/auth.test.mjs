import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { test } from 'node:test'
import ts from 'typescript'
import { createPinia, setActivePinia } from 'pinia'

const url = (source) => 'data:text/javascript;base64,' + Buffer.from(ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
}).outputText).toString('base64')
const source = (file) => readFile(new URL(file, import.meta.url), 'utf8')
const transport = await import(url((await source('../src/api/client.ts')).replace('import.meta.env.VITE_API_BASE', 'undefined')))

test('guest entry prevents rapid duplicate requests and ignores responses after identity expiry', async () => {
  let resolve, calls = 0
  globalThis.__guestApi = { enterGuest: () => { calls++; return new Promise((r) => { resolve = r }) } }
  globalThis.__guestTokens = []
  const text = (await source('../src/stores/auth.ts'))
    .replace("import * as api from '../api/auth'", 'const api = globalThis.__guestApi')
    .replace("import { setCsrfToken } from '../api/client'", 'const setCsrfToken = (value) => globalThis.__guestTokens.push(value)')
    .replace("'pinia'", JSON.stringify(import.meta.resolve('pinia')))
    .replace("'vue'", JSON.stringify(import.meta.resolve('vue')))
  const { useAuthStore } = await import(url(text))
  setActivePinia(createPinia())
  const auth = useAuthStore()
  const first = auth.enterGuest(), duplicate = auth.enterGuest()
  assert.equal(calls, 1); assert.equal(auth.busy, true)
  resolve({ user: { id: 12, is_guest: true }, csrf_token: 'guest-csrf' })
  await Promise.all([first, duplicate])
  assert.equal(auth.user.is_guest, true); assert.equal(auth.busy, false)
  assert.equal(globalThis.__guestTokens.at(-1), 'guest-csrf')
  const late = auth.enterGuest()
  auth.expire()
  resolve({ user: { id: 99, is_guest: true }, csrf_token: 'stale' })
  await late
  assert.equal(auth.user, null)
  assert.equal(globalThis.__guestTokens.at(-1), '')
  delete globalThis.__guestApi; delete globalThis.__guestTokens
})

test('rapid first-question clicks create one session and send only one question', async () => {
  let finishCreate, finishSend, creates = 0, sends = 0
  globalThis.__firstQuestionApi = {
    createSession: () => { creates++; return new Promise((r) => { finishCreate = r }) },
    listMessages: async () => [],
    sendMessage: () => { sends++; return new Promise((r) => { finishSend = r }) },
    listSessions: async () => [{ id: 'guest-first', has_user_message: true }],
  }
  const text = (await source('../src/stores/chat.ts'))
    .replace("import * as api from '../api/chat'", 'const api = globalThis.__firstQuestionApi')
    .replace("'pinia'", JSON.stringify(import.meta.resolve('pinia')))
    .replace("'vue'", JSON.stringify(import.meta.resolve('vue')))
  const { useChatStore } = await import(url(text))
  setActivePinia(createPinia())
  const chat = useChatStore()
  const a = chat.send('first question'), b = chat.send('second click')
  assert.equal(creates, 1)
  finishCreate({ id: 'guest-first', has_user_message: false })
  await new Promise((r) => setImmediate(r))
  assert.equal(sends, 1)
  finishSend(42)
  await Promise.all([a, b])
  delete globalThis.__firstQuestionApi
})

test('credentials included and CSRF supplied on writes', async (t) => {
  transport.setCsrfToken('csrf-alice')
  let sent
  t.mock.method(globalThis, 'fetch', async (_, init) => { sent = init; return Response.json({ ok: true }) })
  await transport.apiPost('/test', {})
  assert.equal(sent.credentials, 'include')
  assert.equal(sent.headers['X-CSRF-Token'], 'csrf-alice')
  transport.setCsrfToken('')
})

test('old-account 401 cannot clear the new-account CSRF', async (t) => {
  let finish
  t.mock.method(globalThis, 'fetch', () => new Promise((resolve) => { finish = resolve }))
  transport.setCsrfToken('alice')
  const oldRequest = transport.apiGet('/old')
  transport.setCsrfToken('bob')
  finish(Response.json({ detail: 'expired' }, { status: 401 }))
  await assert.rejects(oldRequest)
  let sent
  t.mock.method(globalThis, 'fetch', async (_, init) => { sent = init; return Response.json({}) })
  await transport.apiPost('/new', {})
  assert.equal(sent.headers['X-CSRF-Token'], 'bob')
  transport.setCsrfToken('')
})

test('old-account stream callbacks are ignored after switch', async (t) => {
  let controller
  const body = new ReadableStream({ start(c) { controller = c } })
  t.mock.method(globalThis, 'fetch', async () => new Response(body))
  transport.setCsrfToken('alice')
  const received = []
  const pending = transport.streamPost('/old', {}, (event) => received.push(event))
  transport.setCsrfToken('bob')
  controller.enqueue(new TextEncoder().encode('data: {"type":"delta","content":"private"}\n\n'))
  controller.close()
  await pending
  assert.deepEqual(received, [])
  transport.setCsrfToken('')
})

test('late previous-account messages cannot populate reset chat store', async (t) => {
  // Load the actual Pinia store with a controlled API dependency.
  let finish
  globalThis.__accountApi = {
    listMessages: () => new Promise((resolve) => { finish = resolve }),
    listCharacters: async () => [], listSessions: async () => [],
  }
  const storeSource = (await source('../src/stores/chat.ts'))
    .replace("import * as api from '../api/chat'", 'const api = globalThis.__accountApi')
    .replace("'pinia'", JSON.stringify(import.meta.resolve('pinia')))
    .replace("'vue'", JSON.stringify(import.meta.resolve('vue')))
  const { useChatStore } = await import(url(storeSource))
  setActivePinia(createPinia())
  const store = useChatStore()
  const old = store.openSession('alice-chat')
  store.reset()
  finish([{ role: 'user', content: 'alice-private' }])
  await old
  assert.deepEqual(store.messages, [])
  assert.equal(store.activeSessionId, null)
  delete globalThis.__accountApi
})

test('new session blocks rapid clicks and remains disabled until a query is sent', async () => {
  let finishCreate
  let calls = 0
  globalThis.__newChatApi = {
    createSession: () => { calls++; return new Promise((resolve) => { finishCreate = resolve }) },
    listMessages: async () => [],
    sendMessage: async (_, __, handlers) => { handlers.onDelta('answer'); handlers.onDone(9); return 9 },
    listSessions: async () => [{ id: 'empty', has_user_message: true }],
  }
  const storeSource = (await source('../src/stores/chat.ts'))
    .replace("import * as api from '../api/chat'", 'const api = globalThis.__newChatApi')
    .replace("'pinia'", JSON.stringify(import.meta.resolve('pinia')))
    .replace("'vue'", JSON.stringify(import.meta.resolve('vue')))
  const { useChatStore } = await import(url(storeSource))
  setActivePinia(createPinia())
  const store = useChatStore()
  const a = store.newSession(), b = store.newSession()
  assert.equal(calls, 1)
  assert.equal(store.canNewSession, false)
  finishCreate({ id: 'empty', has_user_message: false })
  await Promise.all([a, b])
  await store.newSession()
  assert.equal(calls, 1)
  assert.equal(store.canNewSession, false)
  await store.send('my question')
  assert.equal(store.canNewSession, true)
  // A returned existing empty session is upserted, never duplicated in the list.
  const c = store.newSession()
  finishCreate({ id: 'empty', has_user_message: false })
  await c
  assert.equal(store.sessions.length, 1)
  delete globalThis.__newChatApi
})
