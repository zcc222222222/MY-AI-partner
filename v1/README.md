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

- `DEEPSEEK_API_KEY`:你的密钥。
- `DEEPSEEK_MODEL`:按你账号实际可用模型填。旧代码写的是 `deepseek-v4-pro`,官方常用 `deepseek-chat` / `deepseek-reasoner`,请确认后填写。
- `DATABASE_URL`:默认 SQLite(`sqlite:///./data/app.db`),本地无需 MySQL。

首次启动会自动建表、把 `characters/amis.json` 写入数据库。改角色卡后,把 `version` 字段 +1 重启即可刷新。

> 说明:`.env` 里 `DEEPSEEK_MODEL` 请按你账号实际可用模型填(旧代码写的是 `deepseek-v4-pro`,未必是官方模型名)。当前版本为单用户、无鉴权,适合本地联调。

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

## 待补素材

- `frontend/public/assets/amis-portrait.png`:爱弥斯透明立绘(角色面板用,未放时显示占位提示)。
- 头像已复用旧版 `feixingxuerong.png`(爱弥斯)与 `piaobozhe.jpg`(漂泊者)。
- 可选:背景场景图、favicon / 应用图标。

> 立绘版权提示:使用《鸣潮》官方公开素材(官方 Wiki 立绘、官方公告图)并标注来源,不要用仿绘替代。
