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
    auth_cookie_name: str = os.getenv("AUTH_COOKIE_NAME", "aipartner_session")
    auth_cookie_secure: bool = os.getenv("AUTH_COOKIE_SECURE", "false").lower() == "true"
    auth_session_hours: int = int(os.getenv("AUTH_SESSION_HOURS", "168"))
    auth_rate_limit: int = int(os.getenv("AUTH_RATE_LIMIT", "30"))
    quota_user_daily_questions: int = int(os.getenv('QUOTA_USER_DAILY_QUESTIONS','20'))
    quota_global_daily_questions: int = int(os.getenv('QUOTA_GLOBAL_DAILY_QUESTIONS','200'))
    quota_user_daily_micros: int = int(os.getenv('QUOTA_USER_DAILY_MICROS','500000'))
    quota_global_daily_micros: int = int(os.getenv('QUOTA_GLOBAL_DAILY_MICROS','5000000'))
    quota_global_monthly_micros: int = int(os.getenv('QUOTA_GLOBAL_MONTHLY_MICROS','50000000'))
    quota_user_rpm: int = int(os.getenv('QUOTA_USER_RPM','3'))
    quota_user_concurrency: int = int(os.getenv('QUOTA_USER_CONCURRENCY','1'))
    quota_global_concurrency: int = int(os.getenv('QUOTA_GLOBAL_CONCURRENCY','3'))
    generation_timeout_seconds: int = int(os.getenv('GENERATION_TIMEOUT_SECONDS','300'))
    knowledge_admin_user_ids: tuple = tuple(int(v.strip()) for v in os.getenv("KNOWLEDGE_ADMIN_USER_IDS", "").split(',') if v.strip())
    smtp_host: str = os.getenv("SMTP_HOST", "")
    smtp_port: int = int(os.getenv("SMTP_PORT", "465"))
    smtp_username: str = os.getenv("SMTP_USERNAME", "")
    smtp_password: str = os.getenv("SMTP_PASSWORD", "")
    smtp_from: str = os.getenv("SMTP_FROM", "")
    smtp_security: str = os.getenv("SMTP_SECURITY", "ssl")
    email_code_secret: str = os.getenv("EMAIL_CODE_SECRET", "")

    @property
    def mail_ready(self) -> bool:
        return bool(self.smtp_host and self.smtp_username and self.smtp_password and self.smtp_from
                    and len(self.email_code_secret) >= 32 and self.smtp_security in {"ssl", "starttls"})
    cors_origins: tuple = tuple(
        o.strip()
        for o in os.getenv("CORS_ORIGINS", "http://localhost:5173").split(",")
        if o.strip()
    )
    # 思考模式下temperature不生效；保留该项供未来非思考调用使用。
    llm_temperature: float = float(os.getenv("LLM_TEMPERATURE", "0.9"))
    llm_max_tokens: int = int(os.getenv("LLM_MAX_TOKENS", "4096"))
    llm_reasoning_effort: str = os.getenv("LLM_REASONING_EFFORT", "high")
    llm_final_max_tokens: int = int(os.getenv("LLM_FINAL_MAX_TOKENS", "8192"))
    llm_retry_max_tokens: int = int(os.getenv("LLM_RETRY_MAX_TOKENS", "8192"))
    llm_turn_max_tokens: int = int(os.getenv("LLM_TURN_MAX_TOKENS", "24576"))
    # Local safety envelope, not a claim about the provider's maximum window.
    llm_context_window_tokens: int = int(os.getenv("LLM_CONTEXT_WINDOW_TOKENS", "65536"))
    llm_history_budget: int = int(os.getenv("LLM_HISTORY_BUDGET", "16000"))
    llm_summary_budget: int = int(os.getenv("LLM_SUMMARY_BUDGET", "4000"))
    llm_summary_input_budget: int = int(os.getenv("LLM_SUMMARY_INPUT_BUDGET", "16000"))
    llm_context_safety_tokens: int = 2048

    def __post_init__(self):
        if any(v<=0 for v in (self.quota_user_daily_questions,self.quota_global_daily_questions,
                self.quota_user_daily_micros,self.quota_global_daily_micros,self.quota_global_monthly_micros,
                self.quota_user_rpm,self.quota_user_concurrency,self.quota_global_concurrency,self.generation_timeout_seconds)):
            raise ValueError('额度、并发及生成时限必须为正整数')
        if any(v <= 0 for v in self.knowledge_admin_user_ids):
            raise ValueError('知识管理账号ID须为正整数')
        if self.auth_session_hours <= 0 or self.auth_rate_limit <= 0:
            raise ValueError("登录有效期与认证请求限额必须为正数")
        if self.llm_reasoning_effort not in {"low", "high", "max"}:
            raise ValueError("LLM_REASONING_EFFORT 必须是 low/high/max")
        if any(value <= 0 for value in (self.llm_max_tokens, self.llm_final_max_tokens,
                                        self.llm_retry_max_tokens, self.llm_turn_max_tokens)):
            raise ValueError("生成 token 预算必须为正整数")
        if not (1024 <= self.llm_summary_budget < self.llm_history_budget
                and self.llm_summary_input_budget >= self.llm_summary_budget
                and self.llm_history_budget + max(self.llm_max_tokens, self.llm_final_max_tokens,
                    self.llm_retry_max_tokens) + self.llm_context_safety_tokens < self.llm_context_window_tokens):
            raise ValueError("会话预算必须为摘要、近期历史、回答及协议开销保留空间")

    @property
    def api_key_ready(self) -> bool:
        """密钥是否可用(非空、且不是占位值)。"""
        return _looks_like_real_key(self.deepseek_api_key)


settings = Settings()
