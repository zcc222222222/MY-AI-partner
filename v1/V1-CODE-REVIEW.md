# v1 代码与架构 Review

审阅对象:`v1/`(backend 925 行 / 20 个文件,含 4 个空的 `__init__.py`;frontend 1537 行 / 14 个文件),HEAD = `2554777`。
方法:逐文件通读,然后对**每一条要下结论的问题做独立复核** —— 能跑的就去跑,不靠"看起来像"。复核手段逐条写在结论里。

---

## 一、对照你给 v1 定的 7 条要求

| # | 要求 | 状态 | 依据 |
|---|---|---|---|
| 1 | 基础后端 API(聊天 / 会话、SSE 流式、复用旧流式逻辑) | ✅ | `api/chat.py` 5 个路由;`llm.py:46` 流式封装;`StreamingResponse` + `X-Accel-Buffering: no` |
| 2 | Agent 编排器:DeepSeek function calling,路由到不同「工具 Agent」 | ✅ 但是玩具级 | `agents/orchestrator.py:43` 是真的循环,有 `MAX_TOOL_ROUNDS=2` 和收口重试;工具只有 `get_current_time` / `roll_dice` 两个占位 |
| 3 | 角色扮演 Agent:版本化角色卡 + 记忆 | ⚠️ **只做到一半** | 版本化是真的(`amis.json` 的 `version` + `seed.py:42` 同步)。**记忆没做** —— `roleplay.py:30` 全量回放,见 F2 |
| 4 | 先不做 RAG | ✅ | 全仓无向量库 / 检索依赖;`tools.py` 只留接入点,没有半成品污染 |
| 5 | 接口不堆一个文件、可复用逻辑抽函数、每模块独立文件 | ✅ 基本达成 | 4 路由模块 / 2 服务 / 3 agent 模块。唯一例外见 F4 |
| 6 | 功能别太多,先保证能联调 | ✅ | `schemas.py` 与 `frontend/src/types.ts` **字段逐一对齐**(我比对过);端到端实跑通过(上一轮) |
| 7 | 本地能跑,不强制 MySQL | ✅ | SQLite + WAL,零外部依赖 |

**结论:7 条里 6 条达标,唯一实质性缺口是第 3 条的「记忆」。** 这一条恰好写在你的原始需求里 —— 所以它排在第一位,不是因为"代码不够优雅",而是因为**现在能用,但会在某个长度之后永久坏掉**。

---

## 二、后端:真正该改的

### F2(最高优先)· 历史无上限,会话会在某个长度后**永久**坏掉

`agents/roleplay.py:24-30`:

```python
def build_context(history: list[Any], *, budget: int | None = None) -> list[dict]:
    """v1:全量透传(budget 预留,暂不生效)。"""
    return [{"role": m.role, "content": m.content} for m in history]
```

调用方 `services/chat_service.py:30` 传的是 `session_service.list_messages()`,而那条查询(`session_service.py:49-56`)**没有 LIMIT**。

- 每轮都把整段历史重发一遍 → 输入 token 随轮数近似**平方**增长,直接体现在账单上。
- 更要紧的是:一旦超出上下文窗口就开始报错,而且**不会自愈** —— 历史只增不减,这个会话从此每次必失败。用户看到的是"这个会话坏了",只能删掉重建。
- 你要的是"记忆",不是"全量回放"。要求本来就没打算这么做。

**最小改法(1 行)**:`return [{"role": m.role, "content": m.content} for m in history[-40:]]`

`budget` 参数和 docstring 已经把 v2 的滑窗 + 摘要压缩位置留好了,签名不用动 —— **这个接缝设计是对的,只是没实现**。

> 复核:读了三处源码确认全链路无截断,不是只看那一个函数。

### F1 · 一条代码自己声称、但没有兑现的保证

`api/chat.py:61-63` 的注释写着"此时 200 响应头已经发出去了,再抛异常连接会被硬切断,所以一律降级成 error 事件"——**但 `try:` 从第 72 行才开始**,`SessionLocal()`(`:60`)和两次查库(`:64`、`:68`)都在它外面。

**复核(这条我没靠推理)**:直接读了你 venv 里装的 starlette 1.6.0 源码:

```python
async def stream_response(self, send):
    await send({"type": "http.response.start", ...})   # ← 先发响应头
    async for chunk in self.body_iterator:             # ← 才开始跑生成器
```

顺序确凿。所以 `:60/64/68` 任何一处抛异常(连接池超时、数据库锁超时),客户端拿到的是 **HTTP 200 + 空 body + 没有任何 error 事件**。前端 `client.ts:66` 判 `res.ok` 通过、`chat.ts:127` 也不会触发 —— 界面表现就是"转了一下,什么都没发生",而且**无法从 UI 区分**。

**最小改法**:`try:` 提到第 60 行之前,`except` 保持现在的 error 事件,`finally` 里 `stream_db.close()`。

### F12 · SQLite 路径跟随**当前工作目录**,这个你八成会踩

`config.py:29` 默认 `sqlite:///./data/app.db`,`database.py:15-18` 按这个相对路径建目录;而 `main.py:79` 的 `FRONTEND_DIST` 是 `__file__` 相对的。两者口径不一致。

后果:PyCharm 里若把运行配置的工作目录设成 `v1/`(而不是 `v1/backend/`),uvicorn 会**静默新建一个空的 `v1/data/app.db`** 并重新灌角色卡。没有任何报错,你只会发现**之前的会话全不见了** —— 而它们其实还在 `v1/backend/data/app.db` 里。

**最小改法**:默认值改成从 `__file__` 推导的绝对路径。

### F10 · 改了人设但忘了 `version +1`,会一直用旧的

`seed.py:42` 只在 `version` 变化时才刷新数据库里的角色卡。这正是我上一轮告诉你的用法("改完人设把 version +1 再重启"),所以**设计是故意的**;问题在失败模式**静默** —— 你改完 `amis.json` 重启,面板还显示 v1.0.0,人设却没生效,没有任何提示。

**最小改法**:`elif` 分支里加一句 `logger.warning("角色卡 %s 内容有变化但 version 未变,改动不会生效", slug)`。不改行为,只要别当哑巴。

---

## 三、前端:真正该改的

### G1(P0)· 手机上一行不换的 32 位 ID 把顶栏撑爆,页面横向滚动

`ChatView.vue:39` 把 `chat.activeSessionId` 原样渲染出来,而那是 `uuid4().hex`(`models.py:16`)—— **32 个十六进制字符、没有任何断行机会**。CSS 侧:

- `.chat-header-title`(`main.css:276` 只有 `h1` 规则,**容器本身没有规则**)→ 没有 `min-width: 0`
- `.session-id`(`main.css:283-288`)→ 只有 `font-family/size/color`,**没有 `overflow` / `text-overflow` / `white-space`**
- `.chat-header`(`main.css:265-274`)是 `display:flex` 且不换行

flex 子项默认 `min-width: auto`,**不会收缩到 min-content 以下**。11px 等宽字体 × 32 字符 ≈ 194px,这就是左半边的下限;右侧 `.chat-header-right` 在 ≤760px 时还要显示「新建」+ 会话下拉(≤760px 媒体查询,`main.css:660-683`)。顶栏内容的下限约 **515px**,而手机视口普遍 360–430px。

我确认过 `.app-shell`(`:95-103`)、`.chat-view`(`:258-263`)、`.chat-header` **都没有 `overflow: hidden`** —— 所以内容不是被裁掉,是**真的溢出并产生横向滚动**。这直接违反"移动端不横向滚动"。

**最小改法(两行)**:

```css
.chat-header-title { min-width: 0; }
.session-id { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
```

> 复核:读了三段 CSS + 组件模板 + `models.py:16` 的 id 生成方式,逐项确认;不是照搬结论。

### G2 · 背景 0.6 这层,对 `--muted` 小字**没有**起到它注释里声称的作用

`main.css:76` 写着这层是"文字对比度的保险 —— 背景再亮也不会把面板文字压花"。我**实测**了这句话(不是估算):解出背景视频的 12 帧、按浏览器的方式把 scrim 渐变合成上去、再算 WCAG 对比度。

**页脚输入区那一条带**(x 0.30–0.70,y 0.84–1.0,也就是 `.input-counter` / `.input-error` 真正所在的位置):

| token | 用途 | scrim 0.6 实测最低 | 判定 |
|---|---|---|---|
| `--fg` | 正文 | **8.20:1** | ✅ |
| `--accent` | 强调 | 4.98:1 | ✅ |
| `--muted` | `.input-counter` 12px | **3.72:1** | ❌ 需 4.5 |
| `--danger` | `.input-error` 12px | **2.73:1** | ❌ 需 4.5 |

全画面看更极端:`--muted` 最低 **2.11:1**,有 8.1% 的像素在 4.5:1 以下(源图里确实存在接近纯白的区域,实测像素 `rgb(242,255,255)`)。

**关键的是:把 scrim 调高并不是好解法。** 我试了 0.74:`--muted` 升到 6.01:1 达标,但 `--danger` **仍然只有 4.42:1** —— 也就是说 `--danger` 这个 token 本身在深色底上就偏暗,靠压暗背景救不回来;而且调暗正好和你要的"背景亮一点"相反。

**我建议改成局部兜底,保留 0.6 的亮度**:给 `.chat-input-wrap`(`main.css:421-424`,目前只有 padding 和一条上边框,**没有背景**)加一层不透明/高不透明度的面板,和 `.chat-header`(`:272` 用了 `oklch(0.18 0.024 255 / 0.5)` + 毛玻璃)同一套做法。背景不再参与调制,对比度就变成确定值:

- `--muted` 落到约 **6:1** ✅
- `--danger` 仍偏紧(约 4.3–4.6:1),建议顺手把 `--danger` 提亮到 `oklch(0.72 0.16 22)` 左右,或把错误文案从 12px 提到 13px

顺便:侧栏和角色面板因为自己有 0.55 透明度的面板(`:112`、`:554`),反而**没问题**;消息正文在 `.bubble` 里用的是不透明 `--surface`,也没问题。所以受影响的就是页脚这两行字。

> 复核:写了 `scripts/check-bg-contrast.py` 实测(解码真实视频帧 → 按 CSS 规则合成 scrim → 算 WCAG 比值),命令与输出见附录。这条结论**不是估算出来的**。

### G3 · 中文输入法的回车,可能把半成品发出去

`ChatInput.vue:47` 用 `@keydown.enter.exact.prevent="submit"`。用中文输入法时,回车是**确认候选词**用的;IME 组字期间的 `keydown` 带 `isComposing: true`(旧实现是 `keyCode 229`),而 Vue 的 `.exact` 只判断 Ctrl/Alt/Shift/Meta,**不看 `isComposing`**。

标准守卫:

```ts
function onEnter(e: KeyboardEvent) {
  if (e.isComposing || e.keyCode === 229) return   // 正在组字,这一下回车不是提交
  submit()
}
```

**我没能验证这条**:这台机器没有可用的输入法环境,而我不允许起浏览器做自动化测试;Chrome 各版本在"提交组字的那一下回车"上 `key` 是 `"Enter"` 还是 `"Process"` 有出入。**请你在真实输入法里试一次**:打拼音 → 回车选词,看消息有没有被提前发出去。对一个中文聊天应用,这是每天都在走的路径,值得花这 30 秒。

### G4 · 流没有正常结束时会**静默当成成功**

`api/chat.ts:45-46`:`streamPost` 正常返回后,如果**从头到尾没收到 `done` 事件**(代理超时、后端重启、中间设备返回 200 + HTML),`messageId` 停在 `null`,`streamError` 也是 `null` —— **不抛错**。于是 `stores/chat.ts:120-122` 会把半截回复当完整回复渲染,还照常刷新会话列表。

**最小改法**:`if (!streamError && messageId === null) throw new Error('连接中断,回复可能不完整')` —— 后面 `chat.ts` 现成的错误路径会接管。

### G5 · 会话操作失败时**一个字都不提示**

`stores/chat.ts:51-68`:`newSession` / `openSession` / `removeSession` 三个动作的 rejection 都没人接(`ChatSidebar.vue:39` 甚至没有 `await`),`error.value` 只有 `send()` 会写。所以"新建会话失败""删除失败"在界面上完全无感,只有控制台里有。

**最小改法**:三个动作各自 try/catch 写 `error.value`(抽一个小助手),组件里补 `await`。

---

## 四、其余发现(值得做,不紧急)

| ID | 位置 | 问题 | 最小改法 |
|---|---|---|---|
| F3 | `database.py:20` + `api/chat.py:60` | 每条 SSE 流**全程占住一个连接池连接**;文件型 SQLite 默认 QueuePool size 5 + overflow 10。第 16 条并发流等 30s 后抛 `TimeoutError`,再落进 F1 那条静默路径 | SQLite 用 `poolclass=NullPool`。单机自用撞不到 |
| F4 | `api/characters.py:14-16` | 全项目唯一**绕过 service 层**的查询(要求 5 的例外) | 挪进 `session_service.list_characters()` |
| F5 | `session_service.py:19-26` | `character_id` 传了不存在的值 → **静默回退**到第一个启用角色并返回 200。多角色时这是"我明明选了 A,怎么是 B"的经典来源 | 传了 id 却查不到就抛错,路由映射 404。**真做多角色前必须先修** |
| F6 | `api/chat.py:18-20` vs `:74` | `_sse()` 在流式路径上被绕过(因为 `chat_service` 直接 yield 序列化好的字符串),SSE 帧在**两个地方**拼 | 让 `stream_reply` yield dict,路由统一 `_sse()`;顺带修回"服务层不产出传输格式"这条边界 |
| F9 | `session_service.py:83-84` | 过滤 `\ / : * ? " < > \|` 的循环是**旧版遗留死代码** —— 那是标题当文件名用的年代留下的,现在标题存数据库,这些字符完全合法 | 删掉整个 `for ch in [...]`,保留 `.strip()[:20]` |
| F11 | `models.py:12` + `schemas.py:32-42` | `utcnow()` 存 naive UTC,序列化没有 `Z` 后缀 | **当前无影响**(前端根本没渲染时间戳)。等要做"3 分钟前"必须先修 |
| G6 | `stores/chat.ts:126` | 死分支:`stop()` 必定 `requestSeq += 1`,所以第 124 行的守卫先返回,`AbortError` 判断永远走不到 | 删掉 125-126 行 |
| G7 | `stores/chat.ts:44-49` | `openSession` 无条件赋值 `messages.value`;快速连点会话 A、B,可能显示成"B 的标题配 A 的消息" | await 之后加 `if (activeSessionId.value !== id) return` |
| G8 | `ChatView.vue:16-22` | 自动滚动只认 `streamBuffer + messages.length`:你看历史时每来一个字都被拽到底;而切到消息数相同的会话时它**不触发**,滚动位置还停在上一个会话 | 改 watch 数组 + 仅在接近底部时滚 |
| G9 | `ChatView.vue:42` | 「在线」是写死的,后端连不上时照样显示绿点 | 绑到 `!chat.error && chat.characters.length > 0` |
| G10 | `ChatInput.vue:34-36` | 先把输入框清空再 `chat.send()`,发送失败就把你打的字弄丢了 | `try/catch` 里恢复 `text.value` |
| G11 | `ChatSidebar.vue:35-42` | 删除会话没有二次确认,而且按钮只有约 40px 宽、紧挨标题按钮,一次误触就永久删掉一段对话 | 加一句 `confirm()` |
| G12 | `main.css:613-621,675-683,460-461,495-500` | 触控目标低于 44px:`.ghost` ≈39、移动端 `select` ≈39、`.send` 40、`.bg-toggle` 40×40、`.session-delete` 宽 40。而 ≤760px 时侧栏整个隐藏,**顶栏那两个控件就是唯一的会话入口** | 窄屏断点里统一 `min-height: 44px` |
| G13 | `ChatView.vue:57`、`ChatInput.vue:131` | 流式回答没有 live region(`ChatMessage.vue:22` 那个 `role="status"` 在第一个字到达时就被移除了),错误行也不播报 | `.message-list` 加 `role="log" aria-live="polite"`;错误行加 `role="alert"` |
| G14 | `ChatInput.vue:43-48` | `rows="1"` + `resize:none` → 输入框永远不增高,长文本在单行里滚动,`max-height:140px`(`:447`)是死 CSS | `field-sizing: content` |
| — | `styles/main.css`(706 行) | **占前端总行数的 46%** —— 单个文件吃掉近一半 | C8 拆分的理由成立。但建议等视觉定稿再拆,现在拆只是把 churn 提前 |

---

## 五、我建议 v1 **现在不要做**的东西

你在意"功能先不要太多",这一节和上一节同样重要:

- **Alembic / 迁移**:表就三张且还在动。现在上迁移,改一个字段要写一个版本文件,纯负担。等 schema 稳定、或有不能丢的真实数据时再上。
- **Docker**:本地一条 `uvicorn` 就够了,D1 已经前后端合一。Docker 解决的是"部署到别人机器",那是 P6 的事。
- **鉴权 / 多用户**:没有第二个用户之前,任何鉴权设计都是猜的。而且 F5 说明多角色协议本身还没定 —— 先定协议,再谈多用户。
- **usage 落库 / reasoning 透传**(B6/B2):现在 `logger.info` 已经能看到用量,先看几天数据再决定要不要建表。
- **RAG**:你自己定的 P5,而且 `tools.py` 的接入点是干净的 —— 保持这样。

---

## 六、如果只改三处

1. **F2**(`roleplay.py:30` 加 `[-40:]`)—— 1 行,补上你原始需求里的"记忆",同时去掉平方级成本和"会话永久坏掉"这个失败模式。
2. **G1**(`.chat-header-title` + `.session-id` 各一行 CSS)—— 2 行,消掉唯一一处**确定性**违反"移动端不横向滚动"的问题。
3. **F1**(`api/chat.py` 把 `try:` 上移 3 行)—— 让"失败"重新变成看得见的事件,而不是一片空白。

合计 6 行,不碰任何架构。G2 我建议放在下一批,因为它需要你先看一眼前端的实际观感再定是加面板还是提亮 `--danger`。

---

## 附:这一轮我做了什么、没做什么

**做了**
- 逐文件通读 backend 20 个文件、frontend 14 个文件(行数按字节级换行符计数 —— PowerShell 5.1 的 `Get-Content` 在这种纯 LF 文件上会少数几行,我核对过)。
- F1:读了你 venv 里 starlette 1.6.0 的 `StreamingResponse.stream_response` 源码,确认响应头先于 body 发出。
- G1:读了三段 CSS + 模板 + `models.py` 的 id 生成方式,逐项确认溢出链路(而不是照搬"看起来会溢出")。
- G2:**实测**。新增 `scripts/check-bg-contrast.py`:解码背景视频的 12 帧 → 按 `main.css:77-87` 的规则合成 scrim 渐变 → 用 WCAG 公式算 `--fg/--muted/--accent/--danger` 的对比度,并支持 `--region` 只看页脚那一条带。上面所有数字都是它的输出。
- `git check-ignore` 确认没有 ignore 规则会误伤新加的图标文件。

**没做**
- 没有改动任何 v1 代码 —— 这一轮是 review,不是修改。
- 没有跑 `npm run build`(沙箱不允许 npm 起子进程)。
- 没有用浏览器验证 G3 的中文输入法行为,所以那条明确标成"待你确认"。
- G12 的像素值是从 CSS 读出来的,不是量出来的。

**过程中发现的一个环境坑(与 v1 无关,但你以后自己写脚本会遇到)**:PowerShell 5.1 的 `Get-Content` 在这种纯 LF、无 BOM 的文件上会**少数几行**(`main.css` 它报 702,实际 706)。本文里所有行号按实际换行符计数,我用字节级统计核对过 —— 如果你在编辑器里跳转对不上,以本文为准。
