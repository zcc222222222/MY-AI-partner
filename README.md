# MY-AI-partner · 爱弥斯 AI 助手

《鸣潮》爱弥斯主题的角色扮演聊天机器人。仓库采用**单仓多目录**结构:最初的 Streamlit 单文件版已冻结归档,主线是 `v1/`(FastAPI + Vue 3)。

## 目录一览

| 路径 | 状态 | 说明 |
|---|---|---|
| `legacy/streamlit/` | 🔒 已冻结 | 最初的 Streamlit 版本,只读参考。冻结标签 `v0-streamlit-final` |
| `v1/backend/` | ✅ 主线 | FastAPI + DeepSeek:SSE 流式、function calling 轻量编排、版本化角色卡、SQLite |
| `v1/frontend/` | ✅ 主线 | Vue 3 + TypeScript + Vite + Pinia:深色二次元聊天界面 |

`legacy/` 与 `v1/` 的取舍、以及后续 7 个阶段的演进路线(知识库 / 向量库 / Nginx / MySQL / 可观测性)见架构文档。

## 后端

```powershell
cd v1\backend
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env      # 然后填入 DEEPSEEK_API_KEY
uvicorn app.main:app --reload --port 8000
```

- 首次启动会自动建表,并把 `v1/backend/characters/amis.json` 写入数据库。
- 健康检查:`http://localhost:8000/api/v1/health`;交互式文档:`http://localhost:8000/docs`。
- 本地默认 SQLite(`v1/backend/data/app.db`),**不需要 MySQL**;上线时把 `.env` 的 `DATABASE_URL` 换成 MySQL 连接串即可,ORM 代码不用改。
- `DEEPSEEK_MODEL` 请按账号实际可用模型填写(旧版代码里的 `deepseek-v4-pro` 未必是官方模型名)。

## 前端

```powershell
cd v1\frontend
npm install
npm run dev
```

打开 `http://localhost:5173`。Vite 已把 `/api` 代理到后端 8000 端口,前后端可分开启动。

## 开发约定

1. **新功能一律进 `v1/`**,不要在 `legacy/streamlit/` 里改代码;需要参考旧实现就复制出来改。
2. 旧版若要修安全类问题:从标签切分支 `git checkout -b hotfix/xxx v0-streamlit-final`,改完合回 `legacy/streamlit` 分支,不要直接在 `main` 上动旧代码。
3. 角色人设的唯一来源是 `v1/backend/characters/amis.json`;修改后把 `version` 字段 +1 再重启,即可刷新入库的角色卡。
4. `.env` 不入版本库;新增配置项时同步更新 `v1/backend/.env.example`。

## 素材与版权

角色头像沿用旧版素材(`v1/frontend/public/assets/`)。立绘等《鸣潮》相关素材请使用官方公开图并标注来源,不要用仿绘或生成图替代。
