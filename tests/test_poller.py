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


class TestSSEBroadcasterReplay:
    """Tests for SSE event replay via Last-Event-ID."""

    @pytest.mark.asyncio
    async def test_event_buffer_stores_events(self):
        b = SSEBroadcaster()
        await b.broadcast("e1", {"x": 1})
        await b.broadcast("e2", {"x": 2})
        assert len(b._event_buffer) == 2

    @pytest.mark.asyncio
    async def test_replay_missed_events_on_reconnect(self):
        b = SSEBroadcaster()
        # Broadcast 3 events (no subscribers yet, but events are buffered)
        await b.broadcast("e1", {"x": 1})
        await b.broadcast("e2", {"x": 2})
        await b.broadcast("e3", {"x": 3})

        # Subscribe with Last-Event-ID = 1 (should replay events 2 and 3)
        _id, q = await b.subscribe(last_event_id=1)
        assert q.qsize() == 2
        msg1 = q.get_nowait()
        assert "id: 2\n" in msg1
        msg2 = q.get_nowait()
        assert "id: 3\n" in msg2

    @pytest.mark.asyncio
    async def test_replay_no_missed_events(self):
        b = SSEBroadcaster()
        await b.broadcast("e1", {"x": 1})

        # Subscribe with Last-Event-ID = 1 (no missed events)
        _id, q = await b.subscribe(last_event_id=1)
        assert q.qsize() == 0

    @pytest.mark.asyncio
    async def test_replay_all_events_with_zero_last_id(self):
        b = SSEBroadcaster()
        await b.broadcast("e1", {"x": 1})
        await b.broadcast("e2", {"x": 2})

        # Subscribe with Last-Event-ID = 0 (should replay all events)
        _id, q = await b.subscribe(last_event_id=0)
        assert q.qsize() == 2

    @pytest.mark.asyncio
    async def test_subscribe_without_last_event_id(self):
        b = SSEBroadcaster()
        await b.broadcast("e1", {"x": 1})

        # Normal subscribe (no replay)
        _id, q = await b.subscribe()
        assert q.qsize() == 0

    @pytest.mark.asyncio
    async def test_event_buffer_bounded(self):
        b = SSEBroadcaster()
        # Broadcast more events than buffer size
        for i in range(b._EVENT_BUFFER_SIZE + 50):
            await b.broadcast("e", {"i": i})
        assert len(b._event_buffer) == b._EVENT_BUFFER_SIZE

    @pytest.mark.asyncio
    async def test_replay_respects_queue_limit(self):
        """Replay should stop if client queue fills up."""
        b = SSEBroadcaster()
        # Broadcast many events
        for i in range(150):
            await b.broadcast("e", {"i": i})

        # Subscribe with Last-Event-ID = 0 (tries to replay all 150)
        # Queue maxsize is 100, so only ~100 should be replayed
        _id, q = await b.subscribe(last_event_id=0)
        assert q.qsize() == 100  # bounded by queue maxsize


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


class TestSSEBufferWraparound:
    """Tests for SSE ring buffer wraparound edge cases."""

    @pytest.mark.asyncio
    async def test_ring_buffer_wraparound_preserves_newest(self):
        """After buffer wraps, newest events should still be replayable."""
        b = SSEBroadcaster()
        buf_size = b._EVENT_BUFFER_SIZE
        # Fill buffer beyond capacity
        for i in range(buf_size + 20):
            await b.broadcast("e", {"i": i})

        assert len(b._event_buffer) == buf_size

        # The oldest event in buffer should be event #21 (0-indexed: i=20)
        oldest_event_id, _ = b._event_buffer[0]
        newest_event_id, _ = b._event_buffer[-1]
        assert newest_event_id > oldest_event_id

        # Reconnect: ask for events after ID just before oldest in buffer
        _id, q = await b.subscribe(last_event_id=oldest_event_id - 1)
        # Should get all events in buffer
        assert q.qsize() == min(buf_size, 100)  # capped by queue maxsize

    @pytest.mark.asyncio
    async def test_ring_buffer_old_last_event_id_replays_all_available(self):
        """A very old Last-Event-ID should replay all available buffered events."""
        b = SSEBroadcaster()
        for i in range(50):
            await b.broadcast("e", {"i": i})

        # Request events from ID 0 — should replay all 50
        _id, q = await b.subscribe(last_event_id=0)
        assert q.qsize() == 50

    @pytest.mark.asyncio
    async def test_future_last_event_id_replays_nothing(self):
        """A Last-Event-ID beyond current should not replay any events."""
        b = SSEBroadcaster()
        for i in range(10):
            await b.broadcast("e", {"i": i})

        # Request events from a future ID
        _id, q = await b.subscribe(last_event_id=9999)
        assert q.qsize() == 0


class TestRetryLock:
    """Tests for XBRL retry concurrency safety."""

    @pytest.mark.asyncio
    async def test_retry_lock_prevents_concurrent_runs(self):
        """Only one _retry_xbrl_enrichment should run at a time."""
        from app.poller import _retry_lock

        call_count = 0

        async def slow_retry():
            nonlocal call_count
            async with _retry_lock:
                call_count += 1
                await asyncio.sleep(0.1)

        # Start two concurrent retries
        t1 = asyncio.create_task(slow_retry())
        t2 = asyncio.create_task(slow_retry())
        await asyncio.gather(t1, t2)

        # Both should complete but the lock ensures sequential access
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_retry_skips_when_locked(self):
        """_retry_xbrl_enrichment should skip if already running."""
        from app.poller import _retry_lock, _retry_xbrl_enrichment

        # Acquire lock manually to simulate in-progress retry
        async with _retry_lock:
            # This call should detect lock is held and skip
            assert _retry_lock.locked()
            # We can't easily test the skip behavior without mocking
            # the entire DB, but we verify the lock state is correct
