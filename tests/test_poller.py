"""Tests for the background poller and SSE broadcaster."""

import asyncio

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from unittest.mock import AsyncMock, patch, MagicMock

from app.database import Base
from app.models import Filing
from app.poller import SSEBroadcaster, broadcaster


class TestSSEBroadcaster:
    """Tests for the SSE broadcaster."""

    @pytest.mark.asyncio
    async def test_subscribe_returns_id_and_queue(self):
        b = SSEBroadcaster()
        client_id, q = await b.subscribe()
        assert isinstance(client_id, int)
        assert isinstance(q, asyncio.Queue)
        assert b.client_count == 1

    @pytest.mark.asyncio
    async def test_unsubscribe_removes_client(self):
        b = SSEBroadcaster()
        client_id, q = await b.subscribe()
        assert b.client_count == 1
        await b.unsubscribe(client_id)
        assert b.client_count == 0

    @pytest.mark.asyncio
    async def test_multiple_clients(self):
        b = SSEBroadcaster()
        id1, q1 = await b.subscribe()
        id2, q2 = await b.subscribe()
        id3, q3 = await b.subscribe()
        assert b.client_count == 3
        await b.unsubscribe(id2)
        assert b.client_count == 2

    @pytest.mark.asyncio
    async def test_broadcast_sends_to_all(self):
        b = SSEBroadcaster()
        _id1, q1 = await b.subscribe()
        _id2, q2 = await b.subscribe()

        await b.broadcast("test_event", {"message": "hello"})

        msg1 = q1.get_nowait()
        msg2 = q2.get_nowait()
        assert msg1 == msg2
        assert "test_event" in msg1
        assert "hello" in msg1

    @pytest.mark.asyncio
    async def test_broadcast_format(self):
        b = SSEBroadcaster()
        _id, q = await b.subscribe()

        await b.broadcast("new_filing", {"doc_id": "X1"})

        msg = q.get_nowait()
        assert "id: " in msg
        assert "event: new_filing\n" in msg
        assert "data:" in msg
        assert '"doc_id": "X1"' in msg
        assert msg.endswith("\n\n")

    @pytest.mark.asyncio
    async def test_broadcast_drops_full_queues(self):
        """Full queues should be dropped instead of blocking."""
        b = SSEBroadcaster()
        # Manually inject a bounded queue to test overflow
        bounded_q = asyncio.Queue(maxsize=2)
        b._clients[999] = (bounded_q, 0.0)

        # Fill the queue to capacity
        bounded_q.put_nowait("msg_1")
        bounded_q.put_nowait("msg_2")

        # This should not raise - it should drop the full client
        await b.broadcast("test", {"x": 1})
        assert b.client_count == 0

    @pytest.mark.asyncio
    async def test_broadcast_japanese(self):
        """Japanese characters should be serialized correctly."""
        b = SSEBroadcaster()
        _id, q = await b.subscribe()

        await b.broadcast("filing", {"name": "テスト証券"})

        msg = q.get_nowait()
        assert "テスト証券" in msg


class TestPollerIntegration:
    """Tests for the poll_edinet function."""

    @pytest.mark.asyncio
    async def test_poll_stores_new_filings(self):
        """New filings from EDINET should be stored in DB."""
        from tests.conftest import SAMPLE_EDINET_RESPONSE

        engine = create_async_engine("sqlite+aiosqlite://", echo=False)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

        mock_client = AsyncMock()
        mock_client.fetch_document_list = AsyncMock(
            return_value=[
                r for r in SAMPLE_EDINET_RESPONSE["results"]
                if r["docTypeCode"] in ("350", "360")
            ]
        )
        mock_client.download_xbrl = AsyncMock(return_value=None)

        with patch("app.poller.async_session", session_factory), \
             patch("app.poller.edinet_client", mock_client), \
             patch("app.poller.settings") as mock_settings:
            mock_settings.EDINET_API_KEY = "test"
            mock_settings.LARGE_HOLDING_DOC_TYPES = ["350", "360"]

            from app.poller import poll_edinet
            await poll_edinet()

        # Verify filings were stored
        async with session_factory() as session:
            result = await session.execute(select(Filing))
            filings = result.scalars().all()
            assert len(filings) == 2
            doc_ids = {f.doc_id for f in filings}
            assert "S100ABC1" in doc_ids
            assert "S100ABC2" in doc_ids

        # Verify correct data was stored
        async with session_factory() as session:
            result = await session.execute(
                select(Filing).where(Filing.doc_id == "S100ABC2")
            )
            f = result.scalar_one()
            assert f.filer_name == "ブラックロック・ジャパン株式会社"
            assert f.is_special_exemption is True
            assert "特例対象" in f.doc_description

        await engine.dispose()

    @pytest.mark.asyncio
    async def test_poll_skips_duplicates(self):
        """Polling again should not create duplicate filings."""
        from tests.conftest import SAMPLE_EDINET_RESPONSE

        engine = create_async_engine("sqlite+aiosqlite://", echo=False)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

        filings_data = [
            r for r in SAMPLE_EDINET_RESPONSE["results"]
            if r["docTypeCode"] in ("350", "360")
        ]

        mock_client = AsyncMock()
        mock_client.fetch_document_list = AsyncMock(return_value=filings_data)
        mock_client.download_xbrl = AsyncMock(return_value=None)

        with patch("app.poller.async_session", session_factory), \
             patch("app.poller.edinet_client", mock_client), \
             patch("app.poller.settings") as mock_settings:
            mock_settings.EDINET_API_KEY = "test"
            mock_settings.LARGE_HOLDING_DOC_TYPES = ["350", "360"]

            from app.poller import poll_edinet
            # Poll twice
            await poll_edinet()
            await poll_edinet()

        # Should still only have 2 filings
        async with session_factory() as session:
            result = await session.execute(select(Filing))
            filings = result.scalars().all()
            assert len(filings) == 2

        await engine.dispose()
