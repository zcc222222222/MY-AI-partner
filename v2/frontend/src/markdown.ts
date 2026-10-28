/**
 * Markdown 渲染 —— 只给**助手回复**用。
 *
 * 这里渲染的是模型输出,属于不可信内容,所以安全是第一约束:
 *
 *   1. marked 只负责把 Markdown 变成 HTML,不做任何过滤;
 *   2. DOMPurify 按**白名单**清洗,标签和属性都是逐一列出的 ——
 *      不依赖"默认配置够安全",因为默认配置会随版本变化;
 *   3. 链接统一加 `target=_blank` + `rel=noopener noreferrer`,
 *      避免 `window.opener` 被反向控制。
 *
 * 用 DOMPurify 的钩子而不是改写 marked 的 renderer:renderer 的方法签名在
 * marked 的大版本之间反复变过(18 和 5 就不一样),钩子的接口稳定得多。
 *
 * 用户自己发的消息不经过这里 —— 那是纯文本,让用户输入的 `*` 变成斜体
 * 只会带来困惑。
 */
import DOMPurify from 'dompurify'
import { marked } from 'marked'

marked.setOptions({
  gfm: true,
  // 单个换行当换行处理。原来的纯文本渲染是 white-space: pre-wrap,
  // 不开这项的话聊天里随手换行会全部塌成一行 —— 那是功能倒退。
  breaks: true,
})

// Markdown 实际会产出的标签,逐一列出。多一个都不给。
const ALLOWED_TAGS = [
  'p', 'br', 'hr',
  'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
  'strong', 'em', 'del', 'code', 'pre',
  'blockquote',
  'ul', 'ol', 'li',
  'table', 'thead', 'tbody', 'tr', 'th', 'td',
  'a', 'span',
  'img',
]

const ALLOWED_ATTR = ['href', 'title', 'target', 'rel', 'class', 'align', 'colspan', 'rowspan', 'start', 'src', 'alt', 'loading', 'decoding', 'referrerpolicy']

const PREVIEW_SUFFIX = '?x-oss-process=image/resize,w_1200/format,webp/quality,q_85'

function liveOriginal(src: string): boolean {
  return /^https:\/\/prod-alicdn-community\.kurobbs\.com\/forum\/[A-Za-z0-9/_-]+\.(?:png|jpe?g|webp)$/i.test(src) && !src.includes('..') && !/[\s%\\]/.test(src)
}

function originalSource(src: string): string {
  const original = src.endsWith(PREVIEW_SUFFIX) ? src.slice(0,-PREVIEW_SUFFIX.length) : src
  return liveOriginal(original) || /^\/api\/v1\/knowledge\/images\/[0-9a-f]{64}$/.test(original) ? original : ''
}

DOMPurify.addHook('afterSanitizeAttributes', (node) => {
  if (node.tagName === 'IMG') {
    const src = node.getAttribute('src') || ''
    // The model may supply only validated original URLs; the app adds its one fixed transform.
    if (!liveOriginal(src) && !/^\/api\/v1\/knowledge\/images\/[0-9a-f]{64}$/.test(src)) {
      node.remove()
      return
    }
    if (liveOriginal(src)) node.setAttribute('src',src+PREVIEW_SUFFIX)
    node.setAttribute('loading', 'lazy')
    node.setAttribute('decoding', 'async')
    node.setAttribute('referrerpolicy', 'no-referrer')
    node.setAttribute('tabindex', '0')
    node.setAttribute('role', 'button')
    node.setAttribute('title', '查看原图')
  }
  if (node.tagName === 'A') {
    // 兼容历史回答：采集数据接口应跳到官网对应攻略，而不是展示 JSON。
    try {
      const url = new URL(node.getAttribute('href') || '')
      if (url.hostname === 'guide-server.aki-game.com') {
        const role = url.searchParams.getAll('roleGbId'), guide = url.searchParams.getAll('id')
        if (url.protocol === 'https:' && !url.username && !url.password && !url.port && url.pathname === '/introduction/info'
          && role.length === 1 && guide.length === 1 && /^\d+$/.test(role[0]!) && /^\d+$/.test(guide[0]!)) {
          node.setAttribute('href', `https://mcguide.kurogames.com/?role_id=${role[0]}&strategy_id=${guide[0]}`)
        } else node.removeAttribute('href')
      }
    } catch { /* 相对链接继续使用原有白名单规则。 */ }
    node.setAttribute('target', '_blank')
    node.setAttribute('rel', 'noopener noreferrer')
  }
})

export function handleImageOpen(event: Event): void {
  const target = event.target
  if (target instanceof Element) {
    const button = target.closest('button')
    const image = button ? failedImages.get(button) : undefined
    if (image) {
      event.preventDefault()
      const src = image.getAttribute('src') || ''
      if (!originalSource(src)) return
      image.removeAttribute('data-load-error')
      image.removeAttribute('hidden')
      button?.parentElement?.remove()
      image.removeAttribute('src')
      image.setAttribute('src',src)
      return
    }
  }
  if (!(target instanceof Element) || target.tagName !== 'IMG' || target.closest('a')) return
  const src = originalSource(target.getAttribute('src') || '')
  if (!src) return
  event.preventDefault()
  window.open(src, '_blank', 'noopener,noreferrer')
}

const failedImages = new WeakMap<Element, Element>()

export function handleImageError(event: Event): void {
  const target = event.target
  if (!(target instanceof Element) || target.tagName !== 'IMG' || target.hasAttribute('data-load-error')) return
  const src = target.getAttribute('src') || ''
  const original = originalSource(src)
  // A single fallback, never a retry loop or an invented alternate host.
  if (original && src.endsWith(PREVIEW_SUFFIX)) {
    target.setAttribute('src',original)
    return
  }
  target.setAttribute('data-load-error', '1')
  target.setAttribute('hidden', '')
  const notice = document.createElement('span')
  notice.className = 'image-unavailable'
  notice.textContent = '原图暂无法加载，请稍后重试。'
  const retry = document.createElement('button')
  retry.type = 'button'
  retry.className = 'image-retry'
  retry.textContent = '重试原图'
  failedImages.set(retry,target)
  notice.append(' ',retry)
  target.parentElement?.insertAdjacentElement('afterend', notice)
}

export function renderMarkdown(text: string): string {
  if (!text) return ''
  const html = marked.parse(text) as string
  return DOMPurify.sanitize(html, {
    ALLOWED_TAGS,
    ALLOWED_ATTR,
    // 这些排版属性不是 URL，不应被下面的链接协议规则误删。
    ADD_URI_SAFE_ATTR: ['start', 'align', 'colspan', 'rowspan'],
    // 不允许 data: / javascript: 之类的协议进 href
    ALLOWED_URI_REGEXP: /^(?:https?:|mailto:|#|\/)/i,
  })
}
