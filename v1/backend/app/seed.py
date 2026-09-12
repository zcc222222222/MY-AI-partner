"""把 characters/ 目录下的角色卡 JSON 同步到数据库。"""
import json
import os

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models

CARDS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "characters")


def _load_cards() -> list[dict]:
    cards: list[dict] = []
    if not os.path.isdir(CARDS_DIR):
        return cards
    for filename in sorted(os.listdir(CARDS_DIR)):
        if filename.endswith(".json"):
            with open(os.path.join(CARDS_DIR, filename), encoding="utf-8") as f:
                cards.append(json.load(f))
    return cards


def seed_characters(db: Session) -> None:
    for card in _load_cards():
        existing = db.scalars(
            select(models.Character).where(models.Character.slug == card["slug"])
        ).first()
        if existing is None:
            db.add(
                models.Character(
                    slug=card["slug"],
                    name=card["name"],
                    display_name=card.get("display_name", card["name"]),
                    version=card.get("version", "1.0.0"),
                    avatar=card.get("avatar", ""),
                    greeting=card.get("greeting", ""),
                    system_prompt=card["system_prompt"],
                    personality=card.get("personality", ""),
                )
            )
        elif existing.version != card.get("version", existing.version):
            # 角色卡版本升级时同步刷新
            existing.display_name = card.get("display_name", existing.display_name)
            existing.version = card.get("version", existing.version)
            existing.avatar = card.get("avatar", existing.avatar)
            existing.greeting = card.get("greeting", existing.greeting)
            existing.system_prompt = card["system_prompt"]
            existing.personality = card.get("personality", existing.personality)
    db.commit()
