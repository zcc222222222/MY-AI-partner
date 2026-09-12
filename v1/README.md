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

- `DEEPSEEK_API_KEY`:你的密钥。**注意优先级**:进程环境变量优先于 `.env`(dotenv 默认不覆盖已有变量)。密钥放在系统用户变量里时,从 PyCharm 终端或任意新终端启动都能读到;但若启动它的进程是在你设置变量**之前**启动的(PyCharm 主进程就是这样),它读不到,此时 `.env` 里的占位符会被当成真密钥,聊天会报鉴权失败。最稳的做法是把真密钥写进 `.env`。
- `DEEPSEEK_MODEL`:实测可用的两个取值:
  - `deepseek-chat` —— 普通对话模型,首字延迟低,适合角色扮演聊天。
  - `deepseek-flash` —— **推理模型**:回答前先输出 `reasoning_content`(简单问题也可能思考上千字符),已确认支持 function calling,工具调用链路正常。代价是回答前有几秒静默思考,界面会先显示一个空气泡再逐字出内容。
  - 当前 `llm.py` 只转发 `delta.content`,推理内容被忽略;想让 UI 显示「思考中…」需要额外把它透传出去。
- `DATABASE_URL`:默认 SQLite(`sqlite:///./data/app.db`),本地无需 MySQL。

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

SSE 事件:每条 `data: {"type":"delta","content":"..."}`,结束 `data: {"type":"done","message_id":N}`,异常 `data: {"type":"error","detail":"..."}`。

## 素材

- **全页背景**:`frontend/public/assets/app-background.webp`(2560×1440,带 alpha)。淡浓由 `main.css` 里的 `--bg-image-scrim` 控制:`0` = 原图,`0.9` ≈ 几乎看不见,当前 `0.78`。换图只要覆盖这个文件,或改 `--bg-image` 的路径。
- 头像:复用旧版 `feixingxuerong.png`(爱弥斯)与 `piaobozhe.jpg`(漂泊者)。
- **仍缺** `frontend/public/assets/amis-portrait.png`:角色面板的透明立绘;未放时面板显示占位提示,不会报错。
- 可选:favicon / 应用图标。

> 素材版权提示:使用《鸣潮》官方公开素材(官方 Wiki 立绘、官方公告图)并标注来源,不要用仿绘替代。
