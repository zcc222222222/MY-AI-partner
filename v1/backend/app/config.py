"""应用配置:从环境变量 / .env 读取。"""
import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()

# .env.example 里的占位值。必须和「空」一样当成没配 —— 否则占位符会被当作真密钥
# 发出去,而失败信息会被 api/chat.py 包成 SSE 的 {"type":"error"} 走 HTTP 200,
# 排查成本极高。宁可启动时就拒绝。
_PLACEHOLDER_KEYS = {"sk-你的密钥", "sk-your-key-here", "your-api-key", "changeme"}


def _looks_like_real_key(key: str) -> bool:
    value = (key or "").strip()
    if not value or value in _PLACEHOLDER_KEYS:
        return False
    # DeepSeek 密钥形如 sk- + 32 位以上;明显过短的一定不是真密钥。
    return len(value) >= 20


@dataclass(frozen=True)
class Settings:
    deepseek_api_key: str = os.getenv("DEEPSEEK_API_KEY", "")
    deepseek_base_url: str = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    # 显式写模型名,不要依赖遗留别名(理由见 .env.example)
    deepseek_model: str = os.getenv("DEEPSEEK_MODEL", "deepseek-flash")
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///./data/app.db")
    cors_origins: tuple = tuple(
        o.strip()
        for o in os.getenv("CORS_ORIGINS", "http://localhost:5173").split(",")
        if o.strip()
    )
    # 角色扮演建议 0.8~1.0;未来攻略问答走 0.2~0.3(意图分流见 v2)
    llm_temperature: float = float(os.getenv("LLM_TEMPERATURE", "0.9"))
    llm_max_tokens: int = int(os.getenv("LLM_MAX_TOKENS", "2048"))

    @property
    def api_key_ready(self) -> bool:
        """密钥是否可用(非空、且不是占位值)。"""
        return _looks_like_real_key(self.deepseek_api_key)


settings = Settings()
