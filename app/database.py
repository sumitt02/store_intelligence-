"""
Database setup using SQLAlchemy + SQLite.
SQLite chosen for zero-infra simplicity (documented in CHOICES.md).
WAL mode enabled so reads don't block writes — critical for concurrent API + ingest.
"""
import os
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, DeclarativeBase

_default_db = str(Path(__file__).parent.parent / "data" / "store_intelligence.db")
DB_PATH = os.environ.get("DB_PATH", _default_db)
Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)

DATABASE_URL = f"sqlite:///{DB_PATH}"

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
    pool_pre_ping=True,
)

# Enable WAL mode for concurrent read/write
with engine.connect() as conn:
    conn.execute(text("PRAGMA journal_mode=WAL"))
    conn.execute(text("PRAGMA synchronous=NORMAL"))
    conn.execute(text("PRAGMA cache_size=10000"))
    conn.commit()

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
