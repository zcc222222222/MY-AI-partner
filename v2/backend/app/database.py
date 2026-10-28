"""SQLAlchemy 引擎与会话。

本地默认 SQLite(零安装),上线只需把 DATABASE_URL 换成 MySQL 连接串,
模型层代码无需改动。
"""
import os
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import settings

_is_sqlite = settings.database_url.startswith("sqlite")
_url = make_url(settings.database_url)
if _is_sqlite:
    if _url.database not in (None, "", ":memory:"):
        db_path = Path(_url.database)
        if not db_path.is_absolute():
            db_path = Path(__file__).resolve().parents[1] / db_path
        db_path = db_path.resolve()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        _url = _url.set(database=str(db_path))

engine = create_engine(
    _url,
    connect_args={"check_same_thread": False} if _is_sqlite else {},
    future=True,
    pool_pre_ping=True,
)

if _is_sqlite:

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, _connection_record):
        """check_same_thread=False 只是解除线程断言,并没解决锁竞争。

        这三条才是:
          WAL          —— 读不阻塞写,流式写入期间列表接口仍可读
          busy_timeout —— 撞锁时最多等 5s,而不是立刻抛 database is locked
          foreign_keys —— SQLite 默认不强制外键;打开后由 ORM 的
                          cascade="all, delete-orphan" 保证删会话时子消息先被删掉
        """
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
