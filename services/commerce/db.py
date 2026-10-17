"""中台的数据库装配。

默认 SQLite（零依赖，clone 下来就能跑）；需要真 MySQL 时把 COMMERCE_DB_URL
指向 mysql+pymysql://... 即可，模型层不用改。
"""

from __future__ import annotations

import os
from collections.abc import Generator
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

ROOT = Path(__file__).resolve().parents[2]
DB_PATH = ROOT / "data" / "commerce.db"

DEFAULT_URL = f"sqlite:///{DB_PATH.as_posix()}"
DATABASE_URL = os.getenv("COMMERCE_DB_URL", DEFAULT_URL)

_connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

engine = create_engine(DATABASE_URL, pool_pre_ping=True, future=True, connect_args=_connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_schema(drop_all: bool = False) -> None:
    from services.commerce.models import Base

    if DATABASE_URL.startswith("sqlite"):
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if drop_all:
        Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
