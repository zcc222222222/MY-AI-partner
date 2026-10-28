"""Authenticated, current-source image references; never a general URL proxy."""
from pathlib import Path
import sqlite3

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse

from ..auth import current_user
from knowledge.config import DATA_DIR
from llmwiki import WikiStore, WikiError
from llmwiki.images import resolve_image

WIKI_PATH = Path(DATA_DIR) / "wiki/wiki.db"
router = APIRouter(prefix="/api/v1/knowledge/images", tags=["knowledge"])


@router.get("/{image_id}")
def original_image(image_id: str, principal=Depends(current_user)):
    try:
        image = resolve_image(WikiStore(WIKI_PATH,create=False,read_only=True),image_id)
    except (WikiError, OSError, ValueError, sqlite3.Error):
        image = None
    if image is None:
        raise HTTPException(status_code=404,detail="原图不存在或来源已更新，请重新检索攻略")
    return RedirectResponse(image["url"],status_code=302,headers={"Cache-Control":"no-store","Referrer-Policy":"no-referrer"})
