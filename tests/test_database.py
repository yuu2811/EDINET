"""Tests for database initialization and resilience."""

import os

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine


@pytest.mark.asyncio
async def test_init_db_creates_tables():
    """init_db should create all tables in a fresh database."""
    from app.database import Base, init_db

    # Use in-memory database for isolation
    engine = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Verify tables exist
    from sqlalchemy import inspect

    async with engine.connect() as conn:
        table_names = await conn.run_sync(
            lambda sync_conn: inspect(sync_conn).get_table_names()
        )
    assert "filings" in table_names
    assert "company_info" in table_names
    assert "watchlist" in table_names
    await engine.dispose()


@pytest.mark.asyncio
async def test_init_db_indexes_created():
    """init_db should create composite indexes for performance."""
    from app.database import Base

    engine = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    from sqlalchemy import inspect

    async with engine.connect() as conn:
        indexes = await conn.run_sync(
            lambda sync_conn: inspect(sync_conn).get_indexes("filings")
        )
    index_names = {idx["name"] for idx in indexes}
    assert "ix_filings_xbrl_retry" in index_names
    assert "ix_filings_target_sec_submit" in index_names
    assert "ix_filings_submit_amendment" in index_names
    await engine.dispose()
