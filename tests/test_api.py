"""Tests for FastAPI REST API endpoints."""

import io
import os
import zipfile

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import Base
from app.models import Filing, TenderOffer, Watchlist


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

        # TOB seed data
        tob = TenderOffer(
            doc_id="S100TOB_API",
            edinet_code="E88888",
            filer_name="TOB株式会社",
            doc_type_code="240",
            doc_description="公開買付届出書（トヨタ自動車株式会社）",
            target_company_name="トヨタ自動車株式会社",
            target_sec_code="72030",
            submit_date_time="2026-02-18 11:00",
            pdf_flag=True,
        )
        session.add(tob)
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
        resp = await client.get("/api/filings/S100NOTFOUND")
        assert resp.status_code == 404
        assert resp.json()["error"] == "書類が見つかりません"

    @pytest.mark.asyncio
    async def test_get_filing_invalid_doc_id(self, client):
        """Should reject malformed doc IDs."""
        resp = await client.get("/api/filings/bad-id!")
        assert resp.status_code == 400
        assert "無効な書類ID" in resp.json()["error"]

    @pytest.mark.asyncio
    async def test_filter_by_invalid_date(self, client):
        resp = await client.get("/api/filings?date_from=not-a-date")
        assert resp.status_code == 422
        assert "Invalid" in resp.json()["error"] or "error" in resp.json()

    @pytest.mark.asyncio
    async def test_filter_by_date_range(self, client):
        resp = await client.get(
            "/api/filings?date_from=2026-02-18&date_to=2026-02-18"
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 3

    @pytest.mark.asyncio
    async def test_edinet_url_points_to_viewer(self, client):
        """EDINET URL should point to the EDINET viewer website."""
        resp = await client.get("/api/filings/S100API1")
        data = resp.json()
        assert data["edinet_url"].startswith("https://disclosure2.edinet-fsa.go.jp/")
        assert "S100API1" in data["edinet_url"]

    @pytest.mark.asyncio
    async def test_pdf_url_uses_proxy(self, client):
        """PDF URL should point to our server-side proxy."""
        resp = await client.get("/api/filings/S100API1")
        data = resp.json()
        assert data["pdf_url"] == "/api/documents/S100API1/pdf"


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
        import app.routers.poll as poll_mod
        poll_mod._poll_last_called = 0.0

        resp = await client.post("/api/poll")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "poll_triggered"

    @pytest.mark.asyncio
    async def test_poll_rate_limited(self, client):
        """Rapid polling should be rate-limited (429)."""
        import app.routers.poll as poll_mod
        poll_mod._poll_last_called = 0.0

        resp1 = await client.post("/api/poll")
        assert resp1.status_code == 200

        resp2 = await client.post("/api/poll")
        assert resp2.status_code == 429
        assert "レート制限" in resp2.json()["error"]


class TestPDFProxyEndpoint:
    """Tests for /api/documents/{doc_id}/pdf proxy endpoint."""

    @pytest.mark.asyncio
    async def test_pdf_proxy_success(self, client):
        """Should serve PDF when EDINET API returns valid content."""
        from unittest.mock import patch, AsyncMock

        fake_pdf = b"%PDF-1.4 fake pdf content for testing"
        with patch("app.edinet.edinet_client.download_pdf", new_callable=AsyncMock, return_value=fake_pdf):
            resp = await client.get("/api/documents/S100API1/pdf")

        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/pdf"
        assert resp.content == fake_pdf

    @pytest.mark.asyncio
    async def test_pdf_proxy_invalid_doc_id(self, client):
        """Should reject doc IDs with non-alphanumeric characters."""
        resp = await client.get("/api/documents/bad-id!/pdf")
        assert resp.status_code == 400
        assert "無効な書類ID" in resp.json()["error"]

    @pytest.mark.asyncio
    async def test_pdf_proxy_disclosure2dl_with_leading_whitespace(self, client):
        """Fallback should serve PDF even if header is not at byte 0."""
        from unittest.mock import patch, AsyncMock

        with patch("app.edinet.edinet_client.download_pdf", new_callable=AsyncMock, return_value=None), \
             patch("httpx.AsyncClient") as mock_httpx_cls:
            mock_resp = AsyncMock()
            mock_resp.status_code = 200
            mock_resp.content = b"\xef\xbb\xbf\n%PDF-1.4 fallback pdf"
            mock_hc = AsyncMock()
            mock_hc.get = AsyncMock(return_value=mock_resp)
            mock_hc.__aenter__ = AsyncMock(return_value=mock_hc)
            mock_hc.__aexit__ = AsyncMock(return_value=False)
            mock_httpx_cls.return_value = mock_hc

            resp = await client.get("/api/documents/S100API1/pdf")

        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/pdf"
        assert resp.content == mock_resp.content

    @pytest.mark.asyncio
    async def test_pdf_proxy_redirects_to_edinet_viewer(self, client):
        """When API and disclosure2dl both fail, redirect to EDINET viewer."""
        from unittest.mock import patch, AsyncMock

        with patch("app.edinet.edinet_client.download_pdf", new_callable=AsyncMock, return_value=None), \
             patch("httpx.AsyncClient") as mock_httpx_cls:
            mock_resp = AsyncMock()
            mock_resp.status_code = 404
            mock_resp.content = b"Not Found"
            mock_hc = AsyncMock()
            mock_hc.get = AsyncMock(return_value=mock_resp)
            mock_hc.__aenter__ = AsyncMock(return_value=mock_hc)
            mock_hc.__aexit__ = AsyncMock(return_value=False)
            mock_httpx_cls.return_value = mock_hc

            resp = await client.get(
                "/api/documents/S100API1/pdf",
                follow_redirects=False,
            )

        assert resp.status_code == 302
        assert "disclosure2.edinet-fsa.go.jp" in resp.headers["location"]
        assert "S100API1" in resp.headers["location"]
        data = resp.json()
        assert "redirect_url" in data


class TestAnalyticsAPI:
    """Tests for /api/analytics endpoints."""

    @pytest.mark.asyncio
    async def test_sector_breakdown(self, client):
        """Sector breakdown should aggregate filings by sector prefix."""
        resp = await client.get("/api/analytics/sectors")
        assert resp.status_code == 200
        data = resp.json()
        assert "sectors" in data
        sectors = data["sectors"]
        assert isinstance(sectors, list)
        # Our seed data has filings with target_sec_code "72030" and "67580"
        total_filings = sum(s["filing_count"] for s in sectors)
        assert total_filings >= 2

    @pytest.mark.asyncio
    async def test_sector_breakdown_has_required_fields(self, client):
        """Each sector entry should have sector, company_count, filing_count, avg_ratio."""
        resp = await client.get("/api/analytics/sectors")
        data = resp.json()
        for sector in data["sectors"]:
            assert "sector" in sector
            assert "company_count" in sector
            assert "filing_count" in sector
            assert "avg_ratio" in sector

    @pytest.mark.asyncio
    async def test_rankings(self, client):
        """Rankings endpoint should return structured data."""
        resp = await client.get("/api/analytics/rankings?period=all")
        assert resp.status_code == 200
        data = resp.json()
        assert "most_active_filers" in data
        assert "most_targeted_companies" in data
        assert "largest_increases" in data
        assert "largest_decreases" in data
        assert "busiest_days" in data

    @pytest.mark.asyncio
    async def test_movements(self, client):
        """Market movements endpoint should return structured data."""
        resp = await client.get("/api/analytics/movements?date=2026-02-18")
        assert resp.status_code == 200
        data = resp.json()
        assert data["date"] == "2026-02-18"
        assert "total_filings" in data
        assert "net_direction" in data
        assert "sector_movements" in data

    @pytest.mark.asyncio
    async def test_movements_consolidated_counts(self, client):
        """Consolidated query should compute correct increase/decrease counts."""
        resp = await client.get("/api/analytics/movements?date=2026-02-18")
        data = resp.json()
        assert data["total_filings"] == 3
        # S100API1: 5.12 > 4.80 (increase), S100API2: 6.83 < 7.24 (decrease)
        assert data["increases"] == 1
        assert data["decreases"] == 1
        # unchanged = total - increases - decreases = 3 - 1 - 1 = 1
        assert data["unchanged"] == 1
        assert data["avg_increase"] is not None
        assert data["avg_decrease"] is not None

    @pytest.mark.asyncio
    async def test_movements_empty_date(self, client):
        """Empty date should return zero counts."""
        resp = await client.get("/api/analytics/movements?date=2020-01-01")
        data = resp.json()
        assert data["total_filings"] == 0
        assert data["increases"] == 0
        assert data["decreases"] == 0
        assert data["net_direction"] == "neutral"


class TestStatsCaching:
    """Tests for stats endpoint caching behavior."""

    @pytest.mark.asyncio
    async def test_stats_returns_consistent_data(self, client):
        """Calling stats twice should return same data (from cache)."""
        resp1 = await client.get("/api/stats?date=2026-02-18")
        resp2 = await client.get("/api/stats?date=2026-02-18")
        assert resp1.status_code == 200
        assert resp2.status_code == 200
        d1 = resp1.json()
        d2 = resp2.json()
        assert d1["today_total"] == d2["today_total"]
        assert d1["total_in_db"] == d2["total_in_db"]

    @pytest.mark.asyncio
    async def test_stats_consolidated_counts(self, client):
        """Stats should correctly compute new_reports and amendments."""
        resp = await client.get("/api/stats?date=2026-02-18")
        data = resp.json()
        assert data["today_total"] == 3
        assert data["today_new_reports"] == 2  # API1 + API2
        assert data["today_amendments"] == 1  # API3


class TestTobAPI:
    """Tests for /api/tob endpoint."""

    @pytest.mark.asyncio
    async def test_list_tobs(self, client):
        resp = await client.get("/api/tob")
        assert resp.status_code == 200
        data = resp.json()
        assert "items" in data
        assert "total" in data
        assert data["total"] == 1
        assert data["items"][0]["doc_id"] == "S100TOB_API"
        assert data["items"][0]["tob_type"] == "公開買付届出"

    @pytest.mark.asyncio
    async def test_list_tobs_limit(self, client):
        resp = await client.get("/api/tob?limit=1")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["items"]) == 1

