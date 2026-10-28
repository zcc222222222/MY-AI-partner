/**
 * markdown.ts 的测试 —— 重点是**安全**,不是"能不能渲染"。
 *
 * 为什么值得为它单独测:这里渲染的是**模型输出**,属于不可信内容。
 * 一旦白名单写漏,注入的 HTML 会以 v-html 直接进 DOM。
 *
 * 还有一个更隐蔽的失败模式:DOMPurify 在没有 DOM 时会**静默地把输入原样返回**
 * (isSupported=false),看起来"能用",实际一点都没过滤。所以下面必须有一条
 * 断言证明它真的在拦,而不只是"没报错"。
 *
 * 运行:  npm test        (在 v2/frontend 下)
 * Node 24 能直接执行 .ts(类型擦除),所以不需要额外的构建步骤。
 */
import assert from 'node:assert/strict'
import { test, before } from 'node:test'
import { JSDOM } from 'jsdom'

let renderMarkdown

before(async () => {
  const dom = new JSDOM('<!doctype html><html><body></body></html>')
  globalThis.window = dom.window
  globalThis.document = dom.window.document
  globalThis.Node = dom.window.Node
  globalThis.Element = dom.window.Element
  // 必须在设置好 window 之后再加载,DOMPurify 在导入时就绑定 DOM
  ;({ renderMarkdown } = await import('../src/markdown.ts'))
})

// ---------------------------------------------------------------- 正常渲染

test('加粗 / 斜体 / 行内代码', () => {
  assert.match(renderMarkdown('**粗**'), /<strong>粗<\/strong>/)
  assert.match(renderMarkdown('*斜*'), /<em>斜<\/em>/)
  assert.match(renderMarkdown('`x`'), /<code>x<\/code>/)
})

test('表格(GFM)', () => {
  const html = renderMarkdown('| 项目 | 数值 |\n|---|---|\n| 生命 | 882 |')
  assert.match(html, /<table>/)
  assert.match(html, /<th>项目<\/th>/)
  assert.match(html, /<td>882<\/td>/)
})

test('围栏代码块', () => {
  const html = renderMarkdown('```python\nprint(1)\n```')
  assert.match(html, /<pre><code/)
  assert.match(html, /print\(1\)/)
})

test('列表与引用', () => {
  assert.match(renderMarkdown('- a\n- b'), /<ul>[\s\S]*<li>a<\/li>/)
  assert.match(renderMarkdown('> 引用'), /<blockquote>/)
})

test('攻略标题、嵌套步骤和非1起始编号', () => {
  const html = renderMarkdown('## 输出步骤\n\n3. 第一段\n   - **条件**：满协奏\n4. 第二段')
  assert.match(html, /<h2>输出步骤<\/h2>/)
  assert.match(html, /<ol start="3">/)
  assert.match(html, /<ul>[\s\S]*<strong>条件<\/strong>/)
})

test('流式未闭合的代码围栏及最终正文均可渲染', () => {
  assert.match(renderMarkdown('```python\nif True:\n    print(1)'), /<pre><code[^>]*>if True:\n    print\(1\)/)
  const html = renderMarkdown('```python\nif True:\n    print(1)\n```\n\n**完成**')
  assert.match(html, /<\/pre>[\s\S]*<strong>完成<\/strong>/)
})

test('单换行保留为 <br>(原来的 pre-wrap 行为不能丢)', () => {
  assert.match(renderMarkdown('第一行\n第二行'), /<br\s*\/?>/)
})

test('链接补上 target 与 rel', () => {
  const html = renderMarkdown('[点我](https://example.com)')
  assert.match(html, /href="https:\/\/example\.com"/)
  assert.match(html, /target="_blank"/)
  assert.match(html, /rel="noopener noreferrer"/)
})

test('历史攻略接口链接转换为对应官方网页，不影响普通来源', () => {
  const html = renderMarkdown('[查看来源](https://guide-server.aki-game.com/introduction/info?roleGbId=1210&id=10204)')
  const node = new JSDOM(html).window.document.querySelector('a')
  assert.equal(node.href, 'https://mcguide.kurogames.com/?role_id=1210&strategy_id=10204')
  assert.doesNotMatch(renderMarkdown('[来源](https://guide-server.aki-game.com/introduction/info?roleGbId=1210&id=oops)'), /href=/)
})

// ---------------------------------------------------------------- 安全(核心)

test('清洗器确实在工作 —— 证明它没有静默放行', () => {
  const html = renderMarkdown('<script>alert(1)</script>')
  assert.doesNotMatch(html, /<script/i, 'DOMPurify 没生效(可能 isSupported=false)')
})

test('<script> 被剥离', () => {
  const html = renderMarkdown('你好<script>alert(document.cookie)</script>世界')
  assert.doesNotMatch(html, /script/i)
  assert.doesNotMatch(html, /alert/)
})

test('未经核验的图片地址和事件属性被剥离', () => {
  const html = renderMarkdown('<img src=x onerror="alert(1)">')
  assert.doesNotMatch(html, /onerror/i)
  assert.doesNotMatch(html, /<img/i)
})

test('仅展示本地知识库图片入口，保留懒加载和隐私属性', () => {
  const url='/api/v1/knowledge/images/'+'a'.repeat(64)
  const html=renderMarkdown(`[![武器素材](${url})](${url})`)
  assert.match(html, /<img/)
  assert.match(html, /loading="lazy"/)
  assert.match(html, /decoding="async"/)
  assert.match(html, /referrerpolicy="no-referrer"/)
  assert.match(html, /target="_blank"/)
  for (const bad of ['https://evil.example/x.png','//evil.example/x.png','data:image/png;base64,x',url+'?redirect=evil',url+'x']) {
    assert.doesNotMatch(renderMarkdown(`![x](${bad})`),/<img/i)
  }
  assert.doesNotMatch(renderMarkdown(`<img src="${url}" onerror="alert(1)" style="position:fixed">`),/onerror|position:fixed/)
})

test('原图加载失败有文本退路且不重复插入提示', async () => {
  const { handleImageError }=await import('../src/markdown.ts')
  const container=document.createElement('div')
  container.innerHTML=renderMarkdown(`![原图](/api/v1/knowledge/images/${'b'.repeat(64)})`)
  const img=container.querySelector('img')
  handleImageError({target:img})
  handleImageError({target:img})
  assert.equal(img.hasAttribute('hidden'),true)
  assert.equal(container.querySelectorAll('.image-unavailable').length,1)
  assert.match(container.textContent,/稍后重试/)
  const { handleImageOpen }=await import('../src/markdown.ts')
  const retry=container.querySelector('button')
  handleImageOpen({target:retry,preventDefault(){}})
  assert.equal(img.hasAttribute('hidden'),false)
  assert.equal(img.hasAttribute('data-load-error'),false)
  assert.equal(container.querySelectorAll('.image-unavailable').length,0)
  assert.equal(img.getAttribute('src'),'/api/v1/knowledge/images/'+'b'.repeat(64))
})

test('库街区作者原图允许展示，其他域名路径和主动内容仍被拒绝', () => {
  const base='https://prod-alicdn-community.kurobbs.com/forum/'
  const html=renderMarkdown(`![作者原图](${base}abc123.png)`)
  assert.match(html, /<img/)
  assert.match(html, /referrerpolicy="no-referrer"/)
  assert.match(new JSDOM(html).window.document.querySelector('img').src, /\?x-oss-process=image\/resize,w_1200\/format,webp\/quality,q_85$/)
  for (const bad of [base+'a.svg',base+'a.png?redirect=1',base+'../a.png',
    base.replace('kurobbs.com','kurobbs.com.evil.test')+'a.png',base.replace('/forum/','/avatar/')+'a.png']) {
    assert.doesNotMatch(renderMarkdown(`![x](${bad})`), /<img/i)
  }
})

test('预览失败仅回退一次作者原图，再失败可手动重试', async () => {
  const {handleImageError}=await import('../src/markdown.ts')
  const url='https://prod-alicdn-community.kurobbs.com/forum/a.png'
  const container=document.createElement('div')
  container.innerHTML=renderMarkdown(`![攻略](${url})`)
  const img=container.querySelector('img')
  assert.notEqual(img.getAttribute('src'),url)
  handleImageError({target:img})
  assert.equal(img.getAttribute('src'),url)
  assert.equal(img.hasAttribute('hidden'),false)
  handleImageError({target:img})
  handleImageError({target:img})
  assert.equal(img.hasAttribute('hidden'),true)
  assert.equal(container.querySelectorAll('button').length,1)
})

test('点击作者原图打开图片本身，并限制新窗口权限', async () => {
  const { handleImageOpen }=await import('../src/markdown.ts')
  const container=document.createElement('div')
  const url='https://prod-alicdn-community.kurobbs.com/forum/abc123.png'
  container.innerHTML=renderMarkdown(`![作者原图](${url})`)
  const previous=window.open
  const calls=[]
  window.open=(...args)=>{calls.push(args);return null}
  try { handleImageOpen({target:container.querySelector('img'),preventDefault(){}}) }
  finally { window.open=previous }
  assert.deepEqual(calls,[[url,'_blank','noopener,noreferrer']])
})

test('<iframe> 被剥离', () => {
  const html = renderMarkdown('<iframe src="https://evil.example"></iframe>')
  assert.doesNotMatch(html, /iframe/i)
})

test('事件属性一律剥离(onclick / onmouseover / onload)', () => {
  for (const payload of [
    '<a href="https://ok.example" onclick="alert(1)">x</a>',
    '<div onmouseover="alert(1)">x</div>',
    '<p onload="alert(1)">x</p>',
  ]) {
    const html = renderMarkdown(payload)
    assert.doesNotMatch(html, /\son[a-z]+\s*=/i, `未剥离事件属性: ${payload}`)
  }
})

test('javascript: 协议的链接被拦下', () => {
  const html = renderMarkdown('[点我](javascript:alert(1))')
  assert.doesNotMatch(html, /javascript:/i)
})

test('data: 协议的链接被拦下', () => {
  const html = renderMarkdown('[点我](data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==)')
  assert.doesNotMatch(html, /data:text\/html/i)
})

test('表单与样式注入被剥离', () => {
  const html = renderMarkdown('<form action="/x"><input name="a"></form><style>body{display:none}</style>')
  assert.doesNotMatch(html, /<form/i)
  assert.doesNotMatch(html, /<input/i)
  assert.doesNotMatch(html, /<style/i)
})

test('嵌套绕过尝试(在代码块里塞 script)', () => {
  const html = renderMarkdown('```\n<script>alert(1)</script>\n```')
  // 代码块里应当是**转义后的文本**,不能是可执行的 script 标签
  assert.doesNotMatch(html, /<script/i)
  assert.match(html, /&lt;script&gt;/)
})

test('空输入安全', () => {
  assert.equal(renderMarkdown(''), '')
})
