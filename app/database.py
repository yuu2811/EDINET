import asyncio
import logging
import os

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import settings

logger = logging.getLogger(__name__)

# Ensure the directory for the SQLite database exists
db_url = settings.DATABASE_URL
if "sqlite" in db_url:
    # Extract file path from URL (handle both /// relative and //// absolute)
    db_path = db_url.split("///")[-1]
    if db_path:
        db_dir = os.path.dirname(os.path.abspath(db_path))
        os.makedirs(db_dir, exist_ok=True)

engine = create_async_engine(settings.DATABASE_URL, echo=False)

# Enable WAL mode and busy_timeout for SQLite to avoid "database is locked" errors
if "sqlite" in settings.DATABASE_URL:
    @event.listens_for(engine.sync_engine, "connect")
    def _set_sqlite_pragma(dbapi_conn, connection_record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def init_db():
    """Initialize the database, with corruption recovery for SQLite on /tmp.

    On Render Free plan the SQLite DB lives in /tmp which is ephemeral.
    If the DB file is corrupted (e.g. after a crash), delete it and
    recreate from scratch so the app can still start.
    """
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        # Quick integrity check for SQLite (bounded to 10s to avoid hangs)
        if "sqlite" in settings.DATABASE_URL:
            try:
                async with engine.connect() as conn:
                    result = await asyncio.wait_for(
                        conn.execute(text("PRAGMA integrity_check")),
                        timeout=10.0,
                    )
                    status = result.scalar()
                    if status != "ok":
                        raise RuntimeError(f"SQLite integrity check failed: {status}")
            except asyncio.TimeoutError:
                logger.warning("SQLite integrity check timed out after 10s, skipping")

    except Exception as exc:
        if "sqlite" not in settings.DATABASE_URL:
            raise
        # SQLite on /tmp — attempt recovery by deleting and recreating
        file_path = settings.DATABASE_URL.split("///")[-1]
        if file_path and os.path.exists(file_path):
            logger.warning(
                "Database corrupted or unreadable (%s), removing %s and recreating",
                exc, file_path,
            )
            try:
                os.remove(file_path)
            except OSError:
                pass
            # Dispose the engine so it reconnects to a fresh DB
            await engine.dispose()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("Database recreated after corruption recovery")
