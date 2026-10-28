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
