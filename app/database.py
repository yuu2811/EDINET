import os

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import settings

# Ensure the directory for the SQLite database exists
db_url = settings.DATABASE_URL
if "sqlite" in db_url:
    # Extract file path from URL (handle both /// relative and //// absolute)
    db_path = db_url.split("///")[-1]
    if db_path:
        db_dir = os.path.dirname(os.path.abspath(db_path))
        os.makedirs(db_dir, exist_ok=True)

engine = create_async_engine(settings.DATABASE_URL, echo=False)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Lightweight migration: add columns that may be missing in existing DBs
        await _migrate_add_columns(conn)


async def _migrate_add_columns(conn):
    """Add new columns to existing tables (SQLite-safe)."""
    import sqlalchemy as sa

    migrations = [
        ("filings", "is_demo", "BOOLEAN DEFAULT 0"),
    ]
    for table, column, col_def in migrations:
        try:
            await conn.execute(sa.text(
                f"ALTER TABLE {table} ADD COLUMN {column} {col_def}"
            ))
        except Exception:
            # Column already exists — ignore
            pass
