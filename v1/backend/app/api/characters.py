"""角色卡路由。"""
from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db

router = APIRouter(prefix="/api/v1/characters", tags=["characters"])


@router.get("", response_model=list[schemas.CharacterOut])
def list_characters(db: Session = Depends(get_db)):
    return list(
        db.scalars(select(models.Character).where(models.Character.is_active.is_(True)))
    )
