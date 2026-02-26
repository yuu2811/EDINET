"""Tests for database models."""

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.models import CompanyInfo, Filing, Watchlist


@pytest.mark.asyncio
async def test_filing_to_dict(sample_filing):
    """Filing.to_dict() should return all expected keys."""
    d = sample_filing.to_dict()

    assert d["doc_id"] == "S100TEST1"
    assert d["filer_name"] == "テスト証券株式会社"
    assert d["holder_name"] == "テスト証券株式会社"
    assert d["target_company_name"] == "サンプル工業株式会社"
    assert d["target_sec_code"] == "99990"
    assert d["holding_ratio"] == 5.12
    assert d["previous_holding_ratio"] == 4.80
    assert d["ratio_change"] == pytest.approx(0.32)
    assert d["shares_held"] == 1000000
    assert d["purpose_of_holding"] == "純投資"
    assert d["is_amendment"] is False
    assert d["is_special_exemption"] is False
    assert d["xbrl_parsed"] is True
    assert d["pdf_flag"] is True


@pytest.mark.asyncio
async def test_filing_to_dict_has_urls(sample_filing):
    """Filing.to_dict() should generate EDINET viewer and PDF proxy URLs."""
    d = sample_filing.to_dict()
    # edinet_url now points to the EDINET viewer website
    assert d["edinet_url"].startswith("https://disclosure2.edinet-fsa.go.jp/")
    assert "S100TEST1" in d["edinet_url"]
    # pdf_url still points to our server-side proxy
    assert d["pdf_url"] == "/api/documents/S100TEST1/pdf"


@pytest.mark.asyncio
async def test_filing_ratio_change_none():
    """ratio_change should be None when ratios are missing."""
    filing = Filing(doc_id="X1", holding_ratio=None, previous_holding_ratio=None)
    d = filing.to_dict()
    assert d["ratio_change"] is None


@pytest.mark.asyncio
async def test_filing_ratio_change_negative():
    """ratio_change should be negative when holding decreased."""
    filing = Filing(doc_id="X2", holding_ratio=3.0, previous_holding_ratio=5.0)
    d = filing.to_dict()
    assert d["ratio_change"] == pytest.approx(-2.0)


@pytest.mark.asyncio
async def test_amendment_flag(sample_amendment):
    """Amendment filings should have is_amendment=True."""
    d = sample_amendment.to_dict()
    assert d["is_amendment"] is True
    assert d["doc_type_code"] == "360"


@pytest.mark.asyncio
async def test_filing_persisted(db_session, sample_filing):
    """Filing should be retrievable from DB by doc_id."""
    result = await db_session.execute(
        select(Filing).where(Filing.doc_id == "S100TEST1")
    )
    found = result.scalar_one_or_none()
    assert found is not None
    assert found.filer_name == "テスト証券株式会社"


@pytest.mark.asyncio
async def test_filing_doc_id_unique(db_session, sample_filing):
    """Inserting duplicate doc_id should raise an error."""
    dup = Filing(doc_id="S100TEST1", filer_name="duplicate")
    db_session.add(dup)
    with pytest.raises(Exception):
        await db_session.commit()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_watchlist_to_dict(sample_watchlist_item):
    """Watchlist.to_dict() should return correct data."""
    d = sample_watchlist_item.to_dict()
    assert d["company_name"] == "サンプル工業株式会社"
    assert d["sec_code"] == "99990"
    assert d["edinet_code"] == "E99999"
    assert d["id"] is not None
    assert d["created_at"] is not None


@pytest.mark.asyncio
async def test_watchlist_persisted(db_session, sample_watchlist_item):
    """Watchlist items should be retrievable from DB."""
    result = await db_session.execute(
        select(Watchlist).where(Watchlist.sec_code == "99990")
    )
    found = result.scalar_one_or_none()
    assert found is not None
    assert found.company_name == "サンプル工業株式会社"


# ---------------------------------------------------------------------------
# CompanyInfo model tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_company_info_to_dict_with_bps():
    """CompanyInfo.to_dict() should compute BPS when data is available."""
    ci = CompanyInfo(
        sec_code="1234",
        company_name="テスト株式会社",
        shares_outstanding=1_000_000,
        net_assets=500_000_000,
    )
    d = ci.to_dict()
    assert d["sec_code"] == "1234"
    assert d["company_name"] == "テスト株式会社"
    assert d["shares_outstanding"] == 1_000_000
    assert d["net_assets"] == 500_000_000
    # BPS = 500_000_000 / 1_000_000 = 500.0
    assert d["bps"] == 500.0


@pytest.mark.asyncio
async def test_company_info_bps_none_when_no_shares():
    """BPS should be None when shares_outstanding is missing."""
    ci = CompanyInfo(sec_code="5678", net_assets=100_000_000)
    d = ci.to_dict()
    assert d["bps"] is None


@pytest.mark.asyncio
async def test_company_info_bps_none_when_zero_shares():
    """BPS should be None when shares_outstanding is 0."""
    ci = CompanyInfo(sec_code="5678", shares_outstanding=0, net_assets=100_000_000)
    d = ci.to_dict()
    assert d["bps"] is None


@pytest.mark.asyncio
async def test_company_info_bps_none_when_no_net_assets():
    """BPS should be None when net_assets is missing."""
    ci = CompanyInfo(sec_code="5678", shares_outstanding=1_000_000)
    d = ci.to_dict()
    assert d["bps"] is None
