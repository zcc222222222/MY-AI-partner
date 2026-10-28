const BASE = (import.meta.env.VITE_API_BASE as string | undefined) ?? ''
let csrfToken = ''
export function setCsrfToken(value: string) { csrfToken = value }
export function authExpired() {
  csrfToken = ''
  if (typeof window !== 'undefined') window.dispatchEvent(new Event('auth-expired'))
}

function headers(json = false): Record<string, string> {
  return { ...(json ? { 'Content-Type': 'application/json' } : {}), ...(csrfToken ? { 'X-CSRF-Token': csrfToken } : {}) }
}

/**
 * 把后端的错误响应读成一句能给用户看的话。
 *
 * 注意 Pydantic 的 422 里 `detail` 是**数组**(元素含 loc / msg / type),不是字符串 ——
 * 照抄「取字符串」的写法在 422 上什么也拿不到,用户只会看到「请求失败 (422)」,
 * 完全不知道是字数超了。两个调用口径必须一致,所以抽成一个函数。
 */
async function readErrorDetail(res: Response): Promise<string> {
  let detail = `请求失败 (${res.status})`
  try {
    const data = await res.json()
    if (data && typeof data.detail === 'string') {
      detail = data.detail
    } else if (Array.isArray(data?.detail) && data.detail[0]?.msg) {
      detail = `输入不合法:${data.detail[0].msg}`
    }
  } catch {
    /* 响应体不是 JSON,保留状态码文案 */
  }
  return detail
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const requestToken = csrfToken
  const res = await fetch(`${BASE}${path}`, { ...init, credentials: 'include', headers: { ...headers(), ...init?.headers } })
  if (!res.ok) {
    if (res.status === 401 && requestToken === csrfToken) authExpired()
    throw new Error(await readErrorDetail(res))
  }
  return res.json() as Promise<T>
}

export function apiGet<T>(path: string): Promise<T> {
  return request<T>(path)
}

export function apiPost<T>(path: string, body?: unknown): Promise<T> {
  return request<T>(path, {
    method: 'POST',
    headers: headers(true),
    body: body === undefined ? undefined : JSON.stringify(body),
  })
}

export function apiDelete(path: string): Promise<void> {
  return request<void>(path, { method: 'DELETE' }).then(() => undefined)
}

export interface StreamEvent {
  type: string
  [key: string]: unknown
}

/** 以 SSE 方式 POST,逐行解析 `data: {...}` 并回调。 */
export async function streamPost(
  path: string,
  body: unknown,
  onEvent: (event: StreamEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const requestToken = csrfToken
  const res = await fetch(`${BASE}${path}`, {
    method: 'POST',
    headers: headers(true),
    credentials: 'include',
    body: JSON.stringify(body),
    signal,
  })
  // 以前这里直接抛 `请求失败 (${res.status})`,把后端写的 detail 丢了。
  if (!res.ok) {
    if (res.status === 401 && requestToken === csrfToken) authExpired()
    throw new Error(await readErrorDetail(res))
  }
  if (!res.body) throw new Error('浏览器不支持流式响应')

  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  const handleLine = (line: string) => {
    if (requestToken !== csrfToken) return
    const trimmed = line.trim()
    if (!trimmed.startsWith('data:')) return
    const payload = trimmed.slice(5).trim()
    if (!payload) return
    try {
      onEvent(JSON.parse(payload) as StreamEvent)
    } catch {
      /* 忽略无法解析的行 */
    }
  }

  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    const lines = buffer.split('\n')
    buffer = lines.pop() ?? ''
    for (const line of lines) handleLine(line)
  }
  buffer += decoder.decode()
  if (buffer) handleLine(buffer)
}
