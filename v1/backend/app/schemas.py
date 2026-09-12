"""请求 / 响应模型(Pydantic)。"""
from datetime import datetime

from pydantic import BaseModel, ConfigDict


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
    content: str
