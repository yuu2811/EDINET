"""Tests for FastAPI REST API endpoints."""

import os

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import Base
from app.models import Filing, Watchlist


@pytest_asyncio.fixture
async def api_engine():
    """Create a fresh test database for API tests."""
    engine = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def api_session_factory(api_engine):
    return async_sessionmaker(api_engine, class_=AsyncSession, expire_on_commit=False)


@pytest_asyncio.fixture
async def seed_data(api_session_factory):
    """Insert seed data for API tests."""
    async with api_session_factory() as session:
        filings = [
            Filing(
                doc_id="S100API1",
                filer_name="野村アセット",
                doc_type_code="350",
                doc_description="大量保有報告書",
                holding_ratio=5.12,
                previous_holding_ratio=4.80,
                holder_name="野村アセット",
                target_company_name="トヨタ自動車",
                target_sec_code="72030",
                submit_date_time="2026-02-18 09:15",
                is_amendment=False,
                is_special_exemption=False,
                xbrl_flag=True,
                pdf_flag=True,
            ),
            Filing(
                doc_id="S100API2",
                filer_name="ブラックロック",
                doc_type_code="350",
                doc_description="変更報告書（特例対象株券等）",
                holding_ratio=6.83,
                previous_holding_ratio=7.24,
                holder_name="ブラックロック",
                target_company_name="ソニーグループ",
                target_sec_code="67580",
                submit_date_time="2026-02-18 09:30",
                is_amendment=False,
                is_special_exemption=True,
                xbrl_flag=True,
                pdf_flag=True,
            ),
            Filing(
                doc_id="S100API3",
                filer_name="テスト証券",
                doc_type_code="360",
                doc_description="訂正報告書（大量保有報告書）",
                submit_date_time="2026-02-18 10:00",
                is_amendment=True,
                is_special_exemption=False,
                xbrl_flag=False,
                pdf_flag=True,
            ),
        ]
        for f in filings:
            session.add(f)

        wl = Watchlist(company_name="トヨタ自動車", sec_code="72030")
        session.add(wl)
        await session.commit()


@pytest_asyncio.fixture
async def client(api_session_factory, seed_data):
    """Create a test HTTP client with patched DB."""
    from unittest.mock import patch, AsyncMock

    with patch("app.main.async_session", api_session_factory), \
         patch("app.main.init_db", new_callable=AsyncMock), \
         patch("app.main.run_poller", new_callable=AsyncMock):
        from app.main import app
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


class TestFilingsAPI:
    """Tests for /api/filings endpoint."""

    @pytest.mark.asyncio
    async def test_list_filings(self, client):
        resp = await client.get("/api/filings")
        assert resp.status_code == 200
        data = resp.json()
        assert "filings" in data
        assert "total" in data
        assert data["total"] == 3
        assert len(data["filings"]) == 3

    @pytest.mark.asyncio
    async def test_list_filings_limit(self, client):
        resp = await client.get("/api/filings?limit=1")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["filings"]) == 1
        assert data["total"] == 3

    @pytest.mark.asyncio
    async def test_list_filings_offset(self, client):
        resp = await client.get("/api/filings?offset=2&limit=10")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["filings"]) == 1

    @pytest.mark.asyncio
    async def test_filter_by_filer(self, client):
        resp = await client.get("/api/filings?filer=野村")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["filings"][0]["filer_name"] == "野村アセット"

    @pytest.mark.asyncio
    async def test_filter_by_target(self, client):
        resp = await client.get("/api/filings?target=ソニー")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["filings"][0]["target_company_name"] == "ソニーグループ"

    @pytest.mark.asyncio
    async def test_filter_by_sec_code(self, client):
        resp = await client.get("/api/filings?sec_code=72030")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1

    @pytest.mark.asyncio
    async def test_filter_amendments_only(self, client):
        resp = await client.get("/api/filings?amendment_only=true")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["filings"][0]["is_amendment"] is True

    @pytest.mark.asyncio
    async def test_get_single_filing(self, client):
        resp = await client.get("/api/filings/S100API1")
        assert resp.status_code == 200
        data = resp.json()
        assert data["doc_id"] == "S100API1"
        assert data["holding_ratio"] == 5.12

    @pytest.mark.asyncio
    async def test_get_filing_not_found(self, client):
        resp = await client.get("/api/filings/NONEXISTENT")
        assert resp.status_code == 404
        assert resp.json()["error"] == "Filing not found"

    @pytest.mark.asyncio
    async def test_filter_by_invalid_date(self, client):
        resp = await client.get("/api/filings?date_from=not-a-date")
        assert resp.status_code == 400
        assert "Invalid" in resp.json()["error"]

    @pytest.mark.asyncio
    async def test_filter_by_date_range(self, client):
        resp = await client.get(
            "/api/filings?date_from=2026-02-18&date_to=2026-02-18"
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 3

    @pytest.mark.asyncio
    async def test_edinet_url_no_double_prefix(self, client):
        """EDINET URL should not double the S100 prefix."""
        resp = await client.get("/api/filings/S100API1")
        data = resp.json()
        url = data["edinet_url"]
        assert "S100S100" not in url
        assert url.endswith("S100API1")


class TestStatsAPI:
    """Tests for /api/stats endpoint."""

    @pytest.mark.asyncio
    async def test_get_stats(self, client):
        resp = await client.get("/api/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert "date" in data
        assert "today_total" in data
        assert "today_new_reports" in data
        assert "today_amendments" in data
        assert "total_in_db" in data
        assert "connected_clients" in data
        assert "poll_interval" in data


class TestWatchlistAPI:
    """Tests for /api/watchlist endpoints."""

    @pytest.mark.asyncio
    async def test_get_watchlist(self, client):
        resp = await client.get("/api/watchlist")
        assert resp.status_code == 200
        data = resp.json()
        assert "watchlist" in data
        assert len(data["watchlist"]) == 1
        assert data["watchlist"][0]["company_name"] == "トヨタ自動車"

    @pytest.mark.asyncio
    async def test_add_to_watchlist(self, client):
        resp = await client.post(
            "/api/watchlist",
            json={"company_name": "ソニーグループ", "sec_code": "67580"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["company_name"] == "ソニーグループ"
        assert data["sec_code"] == "67580"
        assert "id" in data

        # Verify it's in the list now
        resp2 = await client.get("/api/watchlist")
        assert len(resp2.json()["watchlist"]) == 2

    @pytest.mark.asyncio
    async def test_add_to_watchlist_missing_name(self, client):
        """Pydantic validation should reject missing company_name."""
        resp = await client.post("/api/watchlist", json={"sec_code": "12340"})
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_add_to_watchlist_empty_name(self, client):
        """Pydantic validation should reject empty company_name."""
        resp = await client.post(
            "/api/watchlist", json={"company_name": ""}
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_delete_from_watchlist(self, client):
        # Get the existing item ID
        resp = await client.get("/api/watchlist")
        item_id = resp.json()["watchlist"][0]["id"]

        # Delete it
        resp2 = await client.delete(f"/api/watchlist/{item_id}")
        assert resp2.status_code == 200

        # Verify it's gone
        resp3 = await client.get("/api/watchlist")
        assert len(resp3.json()["watchlist"]) == 0

    @pytest.mark.asyncio
    async def test_delete_nonexistent(self, client):
        resp = await client.delete("/api/watchlist/99999")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_watchlist_filings_reachable(self, client):
        """GET /api/watchlist/filings must be reachable (not captured by /{item_id})."""
        resp = await client.get("/api/watchlist/filings")
        assert resp.status_code == 200
        data = resp.json()
        assert "filings" in data
        # Should find filings matching watchlist item (トヨタ自動車, 72030)
        assert len(data["filings"]) >= 1
        assert any(
            f["target_sec_code"] == "72030" for f in data["filings"]
        )


class TestWatchlistFilingsAPI:
    """Tests for /api/watchlist/filings endpoint."""

    @pytest.mark.asyncio
    async def test_watchlist_filings_empty_watchlist(self, client):
        """Should return empty list when watchlist is empty."""
        # Delete the existing watchlist item first
        resp = await client.get("/api/watchlist")
        for w in resp.json()["watchlist"]:
            await client.delete(f"/api/watchlist/{w['id']}")

        resp = await client.get("/api/watchlist/filings")
        assert resp.status_code == 200
        assert resp.json()["filings"] == []


class TestSSEEndpoint:
    """Tests for /api/stream SSE endpoint."""

    @pytest.mark.asyncio
    async def test_sse_stream_endpoint_exists(self, client):
        """SSE endpoint should exist and the broadcaster should work independently.

        Note: httpx ASGI transport does not support true SSE streaming.
        SSE broadcast behavior is tested in test_poller.py::TestSSEBroadcaster.
        Here we only verify the route is registered.
        """
        from app.main import app

        routes = [r.path for r in app.routes if hasattr(r, "path")]
        assert "/api/stream" in routes


class TestPollEndpoint:
    """Tests for /api/poll endpoint."""

    @pytest.mark.asyncio
    async def test_trigger_poll(self, client):
        # Reset rate limiter for test
        import app.main as main_mod
        main_mod._poll_last_called = 0.0

        resp = await client.post("/api/poll")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "poll_triggered"

    @pytest.mark.asyncio
    async def test_poll_rate_limited(self, client):
        """Rapid polling should be rate-limited (429)."""
        import app.main as main_mod
        main_mod._poll_last_called = 0.0

        resp1 = await client.post("/api/poll")
        assert resp1.status_code == 200

        resp2 = await client.post("/api/poll")
        assert resp2.status_code == 429
        assert "Rate limited" in resp2.json()["error"]
