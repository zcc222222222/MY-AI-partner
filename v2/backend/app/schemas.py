"""请求 / 响应模型(Pydantic)。"""
from datetime import datetime

import re
from pydantic import BaseModel, ConfigDict, EmailStr, Field, SecretStr, field_validator, model_validator, computed_field

# 单条用户消息的字符上限。前端 ChatInput.vue 的 maxlength 必须与此一致 ——
# 改这里时记得同步改那边(将来抽成配置项后只改一处)。
MAX_MESSAGE_CHARS = 500


class CharacterOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    slug: str
    name: str
    display_name: str
    version: str
    avatar: str
    greeting: str


class SessionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    character_id: int | None = None


class SessionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    title: str
    has_user_message: bool
    created_at: datetime
    updated_at: datetime


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    role: str
    content: str
    created_at: datetime


class MessageCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # min_length 顺手堵住「空消息也触发一次 LLM 调用」的浪费。
    content: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)
    client_request_id: str | None = Field(default=None,min_length=16,max_length=64,pattern=r'^[A-Za-z0-9_-]+$')

    @field_validator("content")
    @classmethod
    def nonblank_content(cls, value):
        if not value.strip():
            raise ValueError("消息不能为空")
        return value


class LoginIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=3, max_length=32, pattern=r"^[a-z0-9_]+$")
    password: SecretStr = Field(min_length=1, max_length=128)

    @field_validator("username", mode="before")
    @classmethod
    def normalize_username(cls, value):
        return value.strip().lower() if isinstance(value, str) else value


class EmailCodeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: EmailStr = Field(max_length=254)

    @field_validator("email", mode="before")
    @classmethod
    def normalize_email(cls, value):
        return value.strip().lower() if isinstance(value, str) else value


class RegisterIn(LoginIn, EmailCodeRequest):
    password: SecretStr = Field(min_length=10, max_length=128)
    confirm_password: SecretStr = Field(min_length=10, max_length=128)
    email_code: str = Field(pattern=r"^[0-9]{6}$")
    display_name: str | None = Field(default=None, min_length=1, max_length=64)

    @model_validator(mode="after")
    def registration_rules(self):
        password = self.password.get_secret_value()
        if not re.search(r"[A-Za-z]", password) or not re.search(r"[0-9]", password):
            raise ValueError("密码须为10–128个字符，且至少包含一个英文字母和一个数字")
        if password != password.strip():
            raise ValueError("密码首尾不能包含空格")
        if password.casefold() in {"password123", "password1234", "password12345", "qwerty12345", "qwerty123456", "abc123456789", "123456789a", "1234567890a"}:
            raise ValueError("密码过于常见，请使用更难猜测的密码")
        if password.casefold() == self.username.casefold():
            raise ValueError("密码不能与用户名相同")
        if password != self.confirm_password.get_secret_value():
            raise ValueError("两次输入的密码不一致")
        return self

    @field_validator("display_name", mode="before")
    @classmethod
    def strip_name(cls, value):
        return value.strip() if isinstance(value, str) else value


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    username: str
    display_name: str

    @computed_field
    @property
    def can_manage_knowledge(self) -> bool:
        from .config import settings
        return self.id in settings.knowledge_admin_user_ids


class AuthOut(BaseModel):
    user: UserOut
    csrf_token: str
