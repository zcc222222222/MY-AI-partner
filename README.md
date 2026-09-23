<div align="center">
  <img src="v1/frontend/public/assets/amis-portrait.jpg" width="190" alt="爱弥斯(飞行雪绒)立绘">
  <h1>爱弥斯 AI 助手</h1>
  <p><b>《鸣潮》爱弥斯主题的角色扮演聊天机器人</b> · v1.0</p>
  <p><code>FastAPI</code> · <code>Vue 3</code> · <code>TypeScript</code> · <code>DeepSeek</code> · <code>SQLite</code></p>
</div>

---

一个从**单文件 Streamlit 脚本**长成**前后端分离应用**的副项目。当前主线是 `v1/`:后端用 FastAPI 提供 SSE 流式对话与 Agent 工具编排,前端用 Vue 3 做二次元风格的聊天界面,角色人设走**版本化角色卡**,本地零外部依赖即可跑通。

> 仓库采用单仓多目录结构:最初的 Streamlit 版本已**冻结归档**在 `legacy/streamlit/`,只作参考,不再改动。

## 目录

- [功能](#功能)
- [技术栈](#技术栈)
- [快速开始](#快速开始)
- [配置项](#配置项)
- [项目结构](#项目结构)
- [API](#api)
- [几个值得说的工程决策](#几个值得说的工程决策)
- [已知限制](#已知限制)
- [路线图:V2](#路线图v2)
- [素材与版权](#素材与版权)
- [许可](#许可)

## 功能

**对话**

- SSE 流式输出,逐字上屏;首字到达前显示角色「正在思考中」
- 可随时**停止生成**;切会话 / 删会话 / 离开页面都会作废在途请求,不会串台
- 输入上限 500 字,前后端同一个「码点」口径(emoji 不会被算成两个字)

**会话**

- 侧栏会话列表:新建、切换、删除
- 首条消息之后用一次额外调用**自动生成会话标题**,且不阻塞界面

**角色**

- 人设来自版本化角色卡 `v1/backend/characters/amis.json`,启动时录入数据库
- 改人设只需改角色卡并把 `version` +1,重启即生效

**Agent**

- 基于 DeepSeek function calling 的轻量编排器:模型决定调工具 → 执行 → 结果回喂 → 继续作答
- 工具是**可插拔的注册表**(`app/agents/tools.py`),v1 内置两个打通链路的示例工具:`get_current_time`、`roll_dice`
- 工具轮次预算耗尽时,**去掉 tools 再收口请求一次**,而不是空手返回

**界面**

- 深色科幻 + 国风衬线的二次元风格,单屏聊天布局,窄屏自动重排(不横向滚动)
- 全页背景 = 静态图打底 + 视频叠层,可**暂停 / 开关声音**;系统开启「减少动态效果」时默认暂停
- 角色立绘面板(展示当前生效的角色卡版本)

**工程**

- **单命令启动**:前端构建一次后,一个 uvicorn 进程同时提供页面和 API
- 启动期校验密钥,空值或占位值**直接拒绝启动**,不留到聊天时才炸
- 每轮记录用量日志(含 `prompt_cache_hit_tokens`,用于监控成本)
- 一组**离线自检脚本**,不需要外网、不需要真密钥

## 技术栈

| 层 | 选型 | 说明 |
|---|---|---|
| 后端 | FastAPI + Uvicorn | 原生 async,SSE 用 `StreamingResponse` |
| LLM | DeepSeek(`openai` SDK) | 走 OpenAI 兼容协议,`base_url` 可替换 |
| ORM / 存储 | SQLAlchemy 2 + SQLite | 开启 WAL;换 MySQL 只需改 `DATABASE_URL` |
| 前端 | Vue 3 + TypeScript + Vite | `<script setup>`、Composition API |
| 状态 | Pinia | 会话、消息、背景播放状态 |
| 样式 | 原生 CSS 变量 | 无 UI 框架,视觉可控 |

## 快速开始

**前置**:Python 3.10+(实测 3.13)、Node 18+(实测 24)、一个可用的 DeepSeek API Key。

### 方式一:单命令(推荐)

一个进程同时提供页面和 API,打开 `http://localhost:8000` 即可。

```bash
# 1) 后端依赖
cd v1/backend
python -m venv .venv
.venv\Scripts\activate            # macOS / Linux: source .venv/bin/activate
pip install -r requirements.txt
copy .env.example .env            # macOS / Linux: cp .env.example .env
# 编辑 .env,填入真实 DEEPSEEK_API_KEY

# 2) 前端构建(只需一次,之后改了前端要重新执行)
cd ../frontend
npm install
npm run build

# 3) 启动(务必在 v1/backend 目录下,见「已知限制」)
cd ../backend
.venv\Scripts\python.exe -m uvicorn app.main:app --port 8000
```

想在同一局域网的其它设备上访问,给 **uvicorn** 加 `--host 0.0.0.0`(不是给 Vite 加)。

### 方式二:前后端分离开发

前端热更新更快,适合改界面。

```bash
# 终端 A
cd v1/backend
.venv\Scripts\python.exe -m uvicorn app.main:app --reload --port 8000

# 终端 B
cd v1/frontend
npm run dev        # → http://localhost:5173(已把 /api 代理到 8000)
```

### 跑一下自检

不需要外网、不需要真密钥:

```bash
python scripts/verify-p0.py          # 后端行为与跨文件一致性
python scripts/verify-failfast.py    # 空密钥必须给出干净的中文启动错误
python scripts/verify-spa-mount.py   # 挂载前端产物后 API 与 /docs 仍可达
python v1/scripts/verify-spa-cache.py  # 静态资源缓存策略(需先 npm run build)
```

## 配置项

全部走环境变量 / `.env`(`v1/backend/.env.example` 是模板,`.env` 不入版本库)。

| 变量 | 默认值 | 说明 |
|---|---|---|
| `DEEPSEEK_API_KEY` | — | **必填**。为空或是模板里的占位值都会导致启动失败 |
| `DEEPSEEK_MODEL` | `deepseek-flash` | 请按账号实际可用模型填写 |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | 任何兼容 OpenAI 协议的服务都可 |
| `DATABASE_URL` | `sqlite:///./data/app.db` | 本地零安装;上线换 MySQL 连接串即可,ORM 代码不动 |
| `CORS_ORIGINS` | `http://localhost:5173` | 逗号分隔;单命令启动同源,用不到 |
| `LLM_TEMPERATURE` | `0.9` | 角色扮演建议 0.8~1.0;将来攻略问答宜 0.2~0.3 |
| `LLM_MAX_TOKENS` | `2048` | 单轮回复上限 |
| `LOG_LEVEL` | `INFO` | 设为 `DEBUG` 可看到外呼细节 |

## 项目结构

```
.
├── v1/                          # 当前主线
│   ├── backend/
│   │   ├── app/
│   │   │   ├── agents/          # 编排器 · 角色扮演 · 工具注册表
│   │   │   ├── api/             # chat / characters / health 路由
│   │   │   ├── services/        # 会话与聊天业务逻辑
│   │   │   ├── config.py        # 环境变量与密钥校验
│   │   │   ├── database.py      # 引擎、SQLite PRAGMA、会话工厂
│   │   │   ├── llm.py           # LLM 客户端(惰性构造)
│   │   │   ├── models.py        # ORM 模型
│   │   │   ├── schemas.py       # 请求/响应模型与长度约束
│   │   │   └── main.py          # 应用装配、启动校验、挂载前端产物
│   │   ├── characters/amis.json # 版本化角色卡
│   │   └── requirements.txt
│   ├── frontend/
│   │   ├── src/
│   │   │   ├── components/      # 侧栏 · 聊天区 · 消息 · 输入框 · 角色面板
│   │   │   ├── stores/          # chat(会话与流式) · background(背景视频)
│   │   │   ├── api/             # HTTP 客户端与 SSE 读取
│   │   │   └── styles/main.css  # 设计令牌与全部样式
│   │   └── public/assets/       # 立绘、背景图/视频、图标
│   └── scripts/                 # 素材工具与 v1 自己的验收脚本
├── legacy/streamlit/            # 🔒 已冻结的初版(标签 v0-streamlit-final)
└── scripts/                     # 后端验收脚本、背景视频压缩与容器探测
```

## API

所有接口在 `/api/v1` 下;启动后可访问 `/docs` 查看交互式文档。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/health` | 健康检查 |
| GET | `/api/v1/characters` | 角色卡列表 |
| GET | `/api/v1/chat/sessions` | 会话列表 |
| POST | `/api/v1/chat/sessions` | 新建会话 |
| DELETE | `/api/v1/chat/sessions/{id}` | 删除会话(子消息级联删除) |
| GET | `/api/v1/chat/sessions/{id}/messages` | 消息列表 |
| POST | `/api/v1/chat/sessions/{id}/messages` | 发消息 → `text/event-stream` |

SSE 事件:

| 事件 | 时机 | 载荷 |
|---|---|---|
| `delta` | 每段正文增量 | `{"type":"delta","content":"..."}` |
| `done` | **回复已完整落库**,前端据此解锁输入框 | `{"type":"done","message_id":N}` |
| `title` | 首条消息之后标题生成完毕 | `{"type":"title","title":"..."}` |
| `error` | 生成失败 | `{"type":"error","detail":"..."}` |

## 几个值得说的工程决策

这些都是实际踩过之后改出来的,不是照模板写的。

**1. system prompt 必须逐字节稳定 —— 这是成本红线。**
DeepSeek 会对重复的 prompt 前缀做自动上下文缓存,命中价约为未命中的 1/10。所以人设之外的东西(时间、检索结果)一律不许拼进 system prompt。V2 接检索时,片段只能作为 **tool result 追加在对话尾部**,否则缓存命中率归零,输入成本直接涨一个量级。

**2. `done` 一定早于 `title`。**
会话标题是首条消息之后的一次额外 LLM 调用。最初它在 `done` 之前,导致回答早已显示完整、输入框却还锁着好几秒。现在 `done` 先发(前端立刻解锁),`title` 作为补发事件跟上,不阻塞界面。

**3. 流式渲染必须更新响应式代理。**
前端最初把消息推进数组后直接改那个裸对象,数据在涨、DOM 一次都不重渲染 —— 表现是「不刷新就看不到回复」。改成 `reactive()` 包装后正常。这类 bug 传输层看不出来,只能在渲染层定位。

**4. 静态资源缓存策略要显式声明。**
FastAPI 默认只发 `ETag` / `Last-Modified`,不发 `Cache-Control`,浏览器于是按「启发式缓存」自行决定新鲜度,期间连条件请求都不发 —— 结果就是「重新构建了前端,浏览器还在用旧页面」,连换 favicon 都不生效。现在带内容哈希的构建产物是 `immutable`,其余一律 `no-cache`(回源校验,命中即 304)。

**5. 输入长度的前后端口径要对齐。**
HTML 的 `maxlength` 按 UTF-16 计数,后端按码点 —— 一个 emoji 就能让两边打架。现在两端统一按码点裁剪与校验。

**6. 工具轮次要能收口,不能空手返回。**
模型可能在工具调用里耗完轮次预算。此时编排器会**去掉 tools 再请求一次**,让模型基于已有信息给出正文;收口后仍无内容则报错且不落库,避免留下一条每次进会话都出现的空气泡。

**7. SQLite 不是「拿来就能用」。**
`check_same_thread=False` 只解除线程断言,并不解决锁竞争。实际需要 `journal_mode=WAL`(读不阻塞写,流式写入期间列表仍可读)+ `busy_timeout=5000`(撞锁等待而不是立刻报错)+ `foreign_keys=ON`(SQLite 默认不强制外键,打开后级联删除才真的生效)。

**8. 宁可启动失败,也不要聊天时才失败。**
密钥为空或是模板占位值时直接拒绝启动,并给出中文提示。否则占位符会被当作真密钥发出去,而错误被 SSE 包成 HTTP 200 的 `error` 事件,排查成本极高。

**9. 背景视频比 GitHub 的限制先到。**
原始素材 3840×2160 / 60fps / 133 MB,超过 GitHub 单文件 100 MB 硬上限。已压成 1080p / 30fps / 12 MB 并加 `+faststart`(moov 移到文件头,浏览器不必下完整个文件才能播第一帧)。

## 已知限制

**这些问题在 v1 里是已知的、有意保留的**,不打算在这个版本解决:

- **单用户、无鉴权**。任何人都能访问你部署的实例,看到全部会话。**不要直接暴露在公网**,局域网内使用请自行判断风险。
- **会话历史全量透传**,没有滑窗或摘要。会话足够长之后会撞上模型上下文上限。改造点已经预留为一个函数签名:`build_context(history, *, budget=None)`。
- **SQLite 走相对路径**(`sqlite:///./data/app.db`),跟随启动时的工作目录 —— 所以必须在 `v1/backend` 下启动,否则会在别处新建一个空库。
- **没有数据库迁移体系**,建表靠 `create_all()`。改表结构目前只能删库重建。
- **没有自动化测试与 CI**。当前靠 `scripts/verify-*.py` 这组离线自检兜底,它们覆盖的是关键行为而不是全量分支。
- **没有知识库**。角色扮演之外的知识全部来自模型自身的参数记忆,关于《鸣潮》的剧情、配队、版本更新等内容**不保证准确**,也没有出处。这是 V2 的首要目标。
- **用量只写日志,没有落库**,因此没有成本看板。
- **没有客户端路由**,所以没做「未知路径回落到 `index.html`」;将来加 vue-router 时必须补上。
- 界面文案、角色卡内容、立绘均为个人项目用法,未做多语言与多角色适配。

## 路线图(V2)

V2 的主题是**把它从「角色扮演 Demo」变成「懂《鸣潮》的助手」**,顺便把工程底座补齐。

**一、知识库与检索(核心)**

- 用 LLM Wiki 的方式整理《鸣潮》资料(剧情、角色、配队、版本更新),从可溯源的公开资料出发,保留出处
- 接入向量库做检索增强:embedding 用 `bge-m3` 或阿里云 DashScope,向量库起步 Qdrant,规模上来后考虑 Milvus
- 在工具注册表里追加 `retrieve_knowledge`、`get_team_build` 等工具 —— 编排入口不变,**前端一行都不用改**
- 意图分流:角色闲聊走高温(0.8~1.0),攻略问答走低温(0.2~0.3)并要求给出处
- 守死第 1 条决策:检索片段作为 tool result 追加在对话尾部,绝不拼进 system prompt

**二、工程底座**

- 引入 Alembic 迁移,摆脱「改表就删库」
- pytest 测试体系 + GitHub Actions CI,把现有离线自检升级为可回归的用例
- 用量落库,做成本与缓存命中率看板

**三、体验与健壮性**

- 长会话:按 token 预算的滑窗 + 摘要压缩(就是那个 `budget` 参数)
- 流异常结束时明确报错,而不是静默当成成功;会话操作失败要有可见提示
- 中文输入法下的回车不再误发半成品
- 窄屏顶栏的会话 ID 换行/截断,消除横向滚动
- 助手回复支持 Markdown 渲染(代码块、列表、表格)
- 背景压暗层对次要文字(小字号、低对比)也要达标,而不是只管正文

**四、上线基础设施**

- **Nginx** 网关:静态资源缓存、SSE 必须关掉 `proxy_buffering`(否则流式会被缓冲成一次性返回)、多个后端实例做负载均衡
- **MySQL + Redis**:MySQL 换掉 SQLite(只需改 `DATABASE_URL`),Redis 承担会话缓存与限流
- **阿里云部署**:ECS + Docker Compose,配 HTTPS 与域名
- **可观测性**:结构化日志 + request ID 贯穿、Prometheus 指标、`/ready` 就绪探针、错误追踪与告警 —— 上线之后得能看见发生了什么

**五、产品化**

- 多用户与鉴权(现在完全没有)
- 多角色:角色卡机制已经是版本化 + 数据驱动的,补一个角色协议与切换入口即可
- 按用户维度的速率限制,避免 Key 被刷

## 素材与版权

- **角色素材**:`v1/frontend/public/assets/` 下的立绘、头像与背景图沿用自旧版素材。**《鸣潮》及角色相关素材版权归库洛游戏(Kuro Games)所有**,本项目为个人学习作品,请勿用于商业用途。
- **背景视频**:原始素材经脚本压缩后入库,压缩参数与容器校验见 `scripts/compress-background-video.ps1` 与 `scripts/probe-mp4.js`。
- **代码**:见下方许可。

## 许可

[MIT](LICENSE) © 2026 zcc222222222

代码可自由使用与修改;**但上述游戏素材不在此许可范围内**,请自行确认素材的授权状态。
