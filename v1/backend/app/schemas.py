"""请求 / 响应模型(Pydantic)。"""
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

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
    character_id: int | None = None


class SessionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    title: str
    created_at: datetime
    updated_at: datetime


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    role: str
    content: str
    created_at: datetime


class MessageCreate(BaseModel):
    # min_length 顺手堵住「空消息也触发一次 LLM 调用」的浪费。
    content: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)
