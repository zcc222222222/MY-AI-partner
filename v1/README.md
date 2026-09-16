# 爱弥斯 AI 助手 · v1.0(本地可跑)

从旧版 Streamlit 单文件脚本浓缩出的前后端分离最小可用版。

> 架构总览见项目根目录 `project-architecture.md`;本文只讲怎么把这个 v1.0 跑起来并和前端联调。本文档与 `project-architecture.md` 是同一套代码的两个视图:一个讲「为什么」,一个讲「怎么跑」。

- **后端**:FastAPI + DeepSeek(SSE 流式 + function calling 轻量编排 + 版本化角色卡 + SQLite)
- **前端**:Vue 3 + TypeScript + Vite + Pinia(深色二次元聊天界面)
- **暂不包含**:RAG / LLM Wiki / 向量库 / Nginx / 多用户鉴权(均留给后续阶段)

> 关于本地是否需要 MySQL:不需要。本地默认 SQLite(`sqlite:///./data/app.db`),开箱即跑;上线时把 `.env` 的 `DATABASE_URL` 换成 MySQL 连接串即可,模型层代码不用改。

## 目录

```
v1/
├── backend/
│   ├── app/
│   │   ├── main.py          # FastAPI 入口
│   │   ├── config.py        # 环境配置
│   │   ├── database.py      # SQLAlchemy(SQLite,可切 MySQL)
│   │   ├── models.py        # 角色卡 / 会话 / 消息
│   │   ├── schemas.py       # Pydantic 模型
│   │   ├── llm.py           # DeepSeek 客户端封装
│   │   ├── seed.py          # 角色卡入库
│   │   ├── agents/          # 编排器 + 角色扮演 + 工具注册表
│   │   ├── services/        # 会话 / 聊天服务
│   │   └── api/             # 路由(chat / characters / health)
│   ├── characters/amis.json # 爱弥斯角色卡(版本化,改这里即可)
│   ├── requirements.txt
│   └── .env.example
└── frontend/
    ├── src/
    │   ├── api/             # fetch + SSE 客户端
    │   ├── stores/chat.ts   # Pinia 状态
    │   ├── components/      # 侧边栏 / 聊天 / 输入 / 角色面板
    │   └── styles/main.css  # 设计令牌
    ├── public/assets/       # 头像(已放入),立绘待补
    ├── package.json
    └── vite.config.ts       # /api 代理到 :8000
```

## 后端启动

```bash
cd v1/backend
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS / Linux:
source .venv/bin/activate

pip install -r requirements.txt

# 复制并填写密钥与模型名
copy .env.example .env      # Windows;Linux/macOS 用 cp .env.example .env

# 启动(在 backend 目录下,保证 ./data 相对路径正确)
uvicorn app.main:app --reload --port 8000
```

`.env` 关键项:

- `DEEPSEEK_API_KEY`:你的密钥。**占位值等于没配** —— 后端启动时会自检,密钥为空或仍是 `.env.example` 里的占位值时**直接拒绝启动**并说明原因(fail fast)。这比让你聊天时才收到一个被 SSE 包起来的鉴权错误好排查得多。
  **优先级提醒**:进程环境变量优先于 `.env`(dotenv 默认不覆盖已有变量),所以把真密钥写进 `.env` 最稳,不受「从哪个终端启动」影响。
- `DEEPSEEK_MODEL`:默认 `deepseek-flash`。实测两种取值:
  - `deepseek-flash` —— **推理模型**:回答前先输出 `reasoning_content`(简单问题也可能思考上千字符),已确认支持 function calling,工具调用链路正常。代价是回答前有几秒静默思考,界面会先显示「飞行雪绒正在思考中」再逐字出内容。
  - `deepseek-chat` —— 普通对话模型,首字延迟低;但它是遗留兼容别名,不建议新环境使用。
  - 当前 `llm.py` 只转发 `delta.content`,推理内容被忽略(UI 的思考提示正好覆盖这段空窗期)。
- `LLM_TEMPERATURE` / `LLM_MAX_TOKENS`:生成参数,默认 `0.9` / `2048`。角色扮演建议 0.8~1.0;将来的攻略问答走 0.2~0.3(意图分流见 v2)。
- `DATABASE_URL`:默认 SQLite(`sqlite:///./data/app.db`),本地无需 MySQL。
- `LOG_LEVEL`:默认 `INFO`。**保持 INFO 才能看到每轮的 token 用量与缓存命中**,见下文 P0-3。

首次启动会自动建表、把 `characters/amis.json` 写入数据库。改角色卡后,把 `version` 字段 +1 重启即可刷新。

> 说明:当前版本为单用户、无鉴权,适合本地联调。实测记录见上文 `DEEPSEEK_MODEL` 一项。

验证:`http://localhost:8000/api/v1/health` 返回 `{"status":"ok"}`,或 `http://localhost:8000/docs` 看交互式 API 文档。

## 前端启动

```bash
cd v1/frontend
npm install
npm run dev
```

打开 `http://localhost:5173`。前端通过 Vite 代理把 `/api` 转发到后端 8000 端口。

> 关于旧应用冻结:见项目根目录架构文档 §8「迁移策略」;简单说就是给旧 commit 打 tag(如 `v0-streamlit`)并把旧文件归档到 `legacy/`,之后只在新目录改,保留随时回退的能力。

## P0 修复记录(第一批)

按 `P0-FIX-LIST.md` 落地。原则是只改这六条,不动整体框架。

| # | 改动 | 位置 |
|---|---|---|
| P0-1 | 默认模型名 → `deepseek-flash`;启动期自检,密钥为空/占位即拒绝启动 | `app/config.py`、`app/main.py`、`.env.example` |
| P0-2 | 工具轮次预算耗尽不再空手返回,改为**去掉 tools 收口请求一次**;收口后仍无正文则报错、不落库 | `app/agents/orchestrator.py`、`app/services/chat_service.py` |
| P0-3 | 采集 `usage`(含 `prompt_cache_hit_tokens`)并逐轮记日志;`temperature`/`max_tokens` 显式化;`build_memory` → `build_context(history, *, budget)` | `app/llm.py`、`app/config.py`、`app/agents/roleplay.py`、`app/services/chat_service.py` |
| P0-4 | `content` 限制 1..500 字;前端 `maxlength` + 接近上限时显示计数 | `app/schemas.py`、`components/ChatInput.vue` |
| P0-5 | 可停止生成(`AbortController` + `requestSeq` 防串台);切会话/删会话/卸载都会作废在途请求 | `stores/chat.ts`、`components/ChatInput.vue`、`App.vue` |
| P0-6 | SQLite 开 WAL + `busy_timeout=5000` + `foreign_keys=ON`;流式接口改用短生命周期探针,不再整段流式占着一条请求级连接 | `app/database.py`、`app/api/chat.py` |

**验收要点**

- 启动日志出现 `LLM 配置就绪 model=deepseek-flash ...`。
- 每轮回答后日志出现 `chat.completed ... usage=[...]`。**连发两轮后 `prompt_cache_hit_tokens` 应 > 0** —— 一直是 0 说明 system prompt 前缀被破坏,输入成本会高出约 10 倍。这是 v2 做检索前必须先确认的健康指标。
- 空消息 / 501 字 → HTTP 422。
- 生成中点「停止」立即停下且不报错;生成中切会话不再串台。
- `v1/backend/data/` 下出现 `app.db-wal`、`app.db-shm`。

> **与清单的两处偏差(经判断后改动)**
> 1. 收口提示用 `role: "user"` 而不是 `"system"` —— 它出现在对话中段,`user` 是所有实现都接受的写法,`system` 中置各家容忍度不一,一旦被拒整条工具链就断了。
> 2. `stream_options`(拿 usage 的前提)加了兜底:端点若不认这个参数,自动去掉它重试一次。宁可丢用量统计,也不让聊天整体不可用。
> 3. 另外补了一处清单没提但会直接导致 P0-3 失效的坑:`main.py` 里加了 `logging.basicConfig(...)`。uvicorn 只给自己的 logger 配 handler,root 没有 handler,应用里的 `logger.info(...)` 会被静默丢掉,「用量日志」根本打不出来。

## 第二批修复记录(遗留问题清单)

| # | 改动 | 位置 |
|---|---|---|
| A1 | LLM 客户端改**惰性构造**:空密钥不再在 import 阶段抛 SDK 的英文 `OpenAIError`(那条报错还叫人去设 `OPENAI_API_KEY`,把排查方向带偏到另一个项目);fail-fast 统一由 lifespan 一处负责 | `app/llm.py` |
| A2 | 流式生成器里会话/角色取值判空,并降级成 `error` 事件。此前 200 响应头已发出,异常会硬切连接,前端表现为「空回答且没有任何报错」 | `app/api/chat.py` |
| A3 | 错误详情统一走 `readErrorDetail`,并处理 Pydantic 422 的**数组** `detail`。此前前端只显示「请求失败 (422)」,不知道是字数超了 | `api/client.ts` |
| A4 | 输入计数与硬挡统一为**码点**口径,与后端 `max_length` 对齐(此前 JS 按 UTF-16 算,一个 emoji 算 2) | `components/ChatInput.vue` |
| B1 | 压掉 `httpx` / `httpcore` / `urllib3` 的 INFO 日志,用量日志不再被外呼日志淹没 | `app/main.py` |
| B3 | LLM 客户端显式 `timeout=60` / `max_retries=2`(SDK 默认 600s,一个挂死的请求占线程 10 分钟) | `app/llm.py` |
| C7 | 补齐 README 引用到的文件 —— 它们此前只存在于设计工作区,仓库里是死链 | `project-architecture.md`、`scripts/` |
| C12 | 前后端字数上限加了跨文件断言,改一头忘另一头会红 | `scripts/verify-p0.py` |

> **A1 的取舍**:清单给的是「把校验前移到 `config.py` 末尾直接 `raise`」。没采纳 —— 那样连 `import app.config` 都会炸(包括将来的 Alembic 和测试),而且 uvicorn 打出来的是一段 import 回溯,不是一句干净的启动错误。惰性构造从根上解决(别在 import 阶段构造客户端),启动判断仍然只有 lifespan 一处。
>
> **A4 的取舍**:清单建议保留 `maxlength`、只把计数改成码点。但 `maxlength` 本身也是 UTF-16 口径,emoji 稍多就会在计数还没到 500 时先把输入拦掉 —— 两个数字自相矛盾。改成 `watch` 里按码点裁剪,硬挡和显示同一个口径。

**本批刻意延后**(不是遗漏,是不适合现在做):
C1 Alembic、B6 usage 落库(依赖 C1 的迁移能力)、B2 reasoning 透传(要先有 B6 的数据)、B4 `/ready`+request ID、B5 安全头、C2/C3 多角色协议与去硬编码、C4 Markdown 渲染、C5 拆层、C6 测试体系、C8 CSS 拆分、C9–C11、D1–D3(单命令启动 / Docker / favicon)。

## 工具脚本

| 文件 | 用途 |
|---|---|
| `scripts/compress-background-video.ps1` | 把背景视频压到 1080p / 30fps / `+faststart` |
| `scripts/probe-mp4.js` | 不依赖 ffmpeg 读 MP4 容器:编码、分辨率、帧率、moov 位置 |
| `scripts/verify-p0.py` | 离线回归检查(不需要外网、不需要真密钥)。`python scripts/verify-p0.py [项目根]` |
| `scripts/verify-failfast.py` | A1 的验收:空密钥必须抛中文 fail-fast,而不是 SDK 的英文报错 |

## 下一步怎么接 RAG(等做 P5 时)

1. 在 `backend/app/agents/tools.py` 的 `TOOLS` / `_HANDLERS` 里追加 `retrieve_knowledge`、`get_team_build` 等工具。
2. 新增 `backend/app/services/knowledge_service.py` 做检索,新增向量库接入。
3. 角色扮演与知识问答共用一个 `run_agent` 编排入口,无需改前端。

## 联调接口一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/health` | 健康检查 |
| GET | `/api/v1/characters` | 角色卡列表 |
| GET | `/api/v1/chat/sessions` | 会话列表 |
| POST | `/api/v1/chat/sessions` | 新建会话 |
| DELETE | `/api/v1/chat/sessions/{id}` | 删除会话 |
| GET | `/api/v1/chat/sessions/{id}/messages` | 消息列表 |
| POST | `/api/v1/chat/sessions/{id}/messages` | 发消息 → SSE 流式 |

SSE 事件:

| 事件 | 何时发出 | 载荷 |
|---|---|---|
| `delta` | 每段正文增量 | `{"type":"delta","content":"..."}` |
| `done` | **回复已完整落库**——前端收到即解锁输入框 | `{"type":"done","message_id":N}` |
| `title` | 首条消息之后,会话标题生成完毕(额外一次 LLM 调用) | `{"type":"title","title":"..."}` |
| `error` | 生成失败 | `{"type":"error","detail":"..."}` |

> `done` 一定在 `title` 之前。标题生成是首条消息才做的额外调用,放在 `done` 之后补发,所以不会让用户多等几秒。

## 素材

- **全页背景(两层)**:底层静态图 `frontend/public/assets/app-background.webp`(2560×1440,带 alpha),上层视频 `frontend/public/assets/app-background.mp4`。淡浓由 `main.css` 里的 `--bg-image-scrim` 控制:`0` = 原图原视频,`0.9` ≈ 几乎看不见,当前 `0.6`。绘制顺序是 `body::before`(静态图)→ `.bg-video`(视频)→ `body::after`(压暗层),`.app-shell` 用 `z-index:1` 浮在最上。
- **视频控制(两个 ghost 按钮)**:输入框右侧依次是「暂停 / 播放」和「声音开关」,逻辑都在 `src/stores/background.ts`。暂停的判定有三个来源:① 用户手动开关;② 标签页切到后台自动暂停、切回自动续播(手动暂停过就不续播);③ 系统开启「减少动态效果」时首次访问默认暂停。素材缺失或解码失败时隐藏视频、露出静态底图,不报错。用户选择存在 `localStorage` 的 `amis.bg-video.paused` 与 `amis.bg-video.sound`。
- **循环播放**由 `<video loop>` 负责,不需要额外配置。注意:源文件首尾本身若不是无缝衔接,循环点会有一处硬切 —— 那是素材问题,代码改不了。
- **为什么一开始听不见声音**:这是浏览器的自动播放策略,不是 bug —— **带声音的视频一律不允许免交互自动播放**,只有 `muted` 的才能自动播。所以 `<video>` 初始是静音,音轨其实一直躺在文件里。点一下右侧的喇叭按钮就能开声(这次点击本身就是浏览器要求的那次「用户交互」),选择会被记住。
  - ⚠️ **单独加一个音频文件解决不了这个问题**:`<audio>` 受同一条策略限制,一样要等用户交互;而且两个媒体元素的时钟会各自漂移,长播放必然不同步。声音就用这条视频自己的音轨。
  - 音量默认压到 **50%**(`background.ts` 的 `BACKGROUND_VOLUME`),因为素材内容我没听过,不想让它一开场就盖过一切。
  - 刷新后如果带声播放仍被浏览器拦下(本次加载还没有过交互),会自动退回**静音播放** —— 画面不受影响,按钮同步显示成静音态,不会把背景整个停掉。
- **背景视频已压缩入库**:原始素材 3840×2160 / 60fps / **48 Mbps** / 133 MB,超过 GitHub 单文件 100 MB 硬上限,直接提交会被拒。已用 `scripts/compress-background-video.ps1` 压成 **1080p / 30fps / 12.06 MB(小了 91%)**,现在随仓库一起走(`public/assets/.gitignore` 里那条 `*.mp4` 已移除)。
- **压缩结果经 `scripts/probe-mp4.js` 回读容器校验**:1920×1080、H.264 `avc1`、695 帧 @30fps、AAC 音轨保留、`faststart: true`(moov 已在文件头,浏览器不必下完整个文件就能播第一帧)。原始文件是 `faststart: false`,moov 在末尾。
- **重新压缩**(换了素材,或想更小):脚本会自动挑项目根目录里最大的 `.mp4` 当输入。

  ```powershell
  # 注意:这台机器没有 pwsh(PowerShell 7),用 powershell -File 或直接 & 调用
  powershell -ExecutionPolicy Bypass -File .\scripts\compress-background-video.ps1 -DryRun
  powershell -ExecutionPolicy Bypass -File .\scripts\compress-background-video.ps1
  ```

  想要更小可以调参:`-Crf 26`(默认 23)、`-Height 720`、`-NoAudio`(去掉音轨,喇叭按钮就没声音了)。压完脚本会自动回读容器校验并对比体积。

- **角色立绘**:`frontend/public/assets/amis-portrait.jpg`(1080×1701)。面板比例已按这个尺寸设为 `1080 / 1701`,所以 `object-fit: contain` 既不裁切也不留黑边;换图若比例不同,记得同步改 `.portrait` 的 `aspect-ratio`。文件缺失时面板显示占位提示,不会报错。
- 头像:复用旧版 `feixingxuerong.png`(爱弥斯)与 `piaobozhe.jpg`(漂泊者)。
- 可选:favicon / 应用图标。

> 素材版权提示:使用《鸣潮》官方公开素材(官方 Wiki 立绘、官方公告图)并标注来源,不要用仿绘替代。
