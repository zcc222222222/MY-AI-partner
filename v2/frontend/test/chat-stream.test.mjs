import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { test } from 'node:test'
import ts from 'typescript'

// Compile the real modules with Vite's env replacement; the transport is real,
// only fetch is mocked. No browser or running backend is required.
const dataUrl = (source) => 'data:text/javascript;base64,' + Buffer.from(
  ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } }).outputText,
).toString('base64')
const client = (await readFile(new URL('../src/api/client.ts', import.meta.url), 'utf8'))
  .replace('import.meta.env.VITE_API_BASE', 'undefined')
const chat = (await readFile(new URL('../src/api/chat.ts', import.meta.url), 'utf8'))
  .replace("'./client'", JSON.stringify(dataUrl(client)))
const { sendMessage } = await import(dataUrl(chat))

function mockStream(events) {
  return async () => new Response(events.map((e) => `data: ${JSON.stringify(e)}\n\n`).join(''), {
    headers: { 'Content-Type': 'text/event-stream' },
  })
}

test('new sends use distinct request ids and explicit retries retain their id', async (t) => {
  const ids = []
  t.mock.method(globalThis, 'fetch', async (_, options) => {
    ids.push(JSON.parse(options.body).client_request_id)
    return new Response('data: {"type":"done","message_id":7}\n\n', { headers: { 'Content-Type':'text/event-stream' } })
  })
  await sendMessage('s','q',{ onDelta() {} })
  await sendMessage('s','q',{ onDelta() {} })
  await sendMessage('s','q',{ onDelta() {} },undefined,'explicit-retry-id-0001')
  await sendMessage('s','q',{ onDelta() {} },undefined,'explicit-retry-id-0001')
  assert.notEqual(ids[0],ids[1]); assert.equal(ids[2],ids[3])
})

test('quota refusal surfaces server explanation without accepting an empty answer', async (t) => {
  t.mock.method(globalThis,'fetch',async()=>new Response(JSON.stringify({ detail:'今日提问额度已用完' }),{ status:429 }))
  await assert.rejects(sendMessage('s','q',{ onDelta() {} }),/今日提问额度已用完/)
})

test('valid SSE done returns persisted id', async (t) => {
  t.mock.method(globalThis, 'fetch', mockStream([{ type: 'delta', content: 'answer' }, { type: 'done', message_id: 7 }]))
  let text = ''
  assert.equal(await sendMessage('session', 'q', { onDelta: (delta) => { text += delta } }), 7)
  assert.equal(text, 'answer')
})

test('real tool progress is delivered separately from answer text', async (t) => {
  t.mock.method(globalThis, 'fetch', mockStream([
    { type: 'tool_status', message: '正在查询库街区攻略…' },
    { type: 'delta', content: '攻略正文' }, { type: 'done', message_id: 9 },
  ]))
  const statuses=[]; let text=''
  await sendMessage('session','q',{onDelta(value){text+=value},onStatus(value){statuses.push(value)}})
  assert.deepEqual(statuses,['正在查询库街区攻略…'])
  assert.equal(text,'攻略正文')
})

test('silent SSE EOF after partial text is rejected', async (t) => {
  t.mock.method(globalThis, 'fetch', mockStream([{ type: 'delta', content: 'partial' }]))
  await assert.rejects(sendMessage('session', 'q', { onDelta() {} }), /未收到完整回答/)
})

test('explicit generation error remains visible', async (t) => {
  t.mock.method(globalThis, 'fetch', mockStream([{ type: 'delta', content: 'partial' }, { type: 'error', detail: 'token 上限' }]))
  await assert.rejects(sendMessage('session', 'q', { onDelta() {} }), /token 上限/)
})

test('budget fallback replaces partial text instead of appending to it', async (t) => {
  t.mock.method(globalThis, 'fetch', mockStream([
    { type: 'delta', content: 'unfinished advice' },
    { type: 'replace', content: '回答未完成，请拆小问题。' },
    { type: 'done', message_id: 8, status: 'fallback' },
  ]))
  let text = ''
  assert.equal(await sendMessage('session', 'q', {
    onDelta: (delta) => { text += delta },
    onReplace: (replacement) => { text = replacement },
  }), 8)
  assert.equal(text, '回答未完成，请拆小问题。')
})
