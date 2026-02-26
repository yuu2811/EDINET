"""Shared fixtures for tests."""

import asyncio
import os

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# Force in-memory test database before any app imports
os.environ["DATABASE_URL"] = "sqlite+aiosqlite://"
os.environ["EDINET_API_KEY"] = "test_api_key_for_testing"
os.environ["POLL_INTERVAL"] = "9999"

from app.database import Base
from app.models import Filing, TenderOffer, Watchlist


@pytest.fixture(scope="session")
def event_loop():
    """Create a session-scoped event loop."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture
async def db_engine():
    """Create a fresh in-memory test database for each test."""
    engine = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(db_engine):
    """Provide a transactional database session for tests."""
    session_factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        yield session


@pytest_asyncio.fixture
async def sample_filing(db_session):
    """Insert a sample filing for tests."""
    filing = Filing(
        doc_id="S100TEST1",
        seq_number=1,
        edinet_code="E12345",
        filer_name="テスト証券株式会社",
        sec_code="12340",
        doc_type_code="350",
        ordinance_code="060",
        form_code="010000",
        doc_description="大量保有報告書",
        subject_edinet_code="E99999",
        issuer_edinet_code="E99999",
        holding_ratio=5.12,
        previous_holding_ratio=4.80,
        holder_name="テスト証券株式会社",
        target_company_name="サンプル工業株式会社",
        target_sec_code="99990",
        shares_held=1000000,
        purpose_of_holding="純投資",
        submit_date_time="2026-02-18 09:15",
        xbrl_flag=True,
        pdf_flag=True,
        is_amendment=False,
        is_special_exemption=False,
        xbrl_parsed=True,
    )
    db_session.add(filing)
    await db_session.commit()
    await db_session.refresh(filing)
    return filing


@pytest_asyncio.fixture
async def sample_amendment(db_session):
    """Insert a sample amendment filing."""
    filing = Filing(
        doc_id="S100TEST2",
        seq_number=2,
        edinet_code="E12345",
        filer_name="テスト証券株式会社",
        sec_code="12340",
        doc_type_code="360",
        ordinance_code="060",
        form_code="010000",
        doc_description="訂正報告書（大量保有報告書・変更報告書）",
        subject_edinet_code="E99999",
        submit_date_time="2026-02-18 10:30",
        xbrl_flag=True,
        pdf_flag=True,
        is_amendment=True,
        is_special_exemption=False,
    )
    db_session.add(filing)
    await db_session.commit()
    await db_session.refresh(filing)
    return filing


@pytest_asyncio.fixture
async def sample_tob(db_session):
    """Insert a sample tender offer filing."""
    tob = TenderOffer(
        doc_id="S100TOB01",
        edinet_code="E77777",
        filer_name="TOBアクイジション株式会社",
        sec_code="77770",
        doc_type_code="240",
        doc_description="公開買付届出書（サンプル工業株式会社）",
        subject_edinet_code="E99999",
        issuer_edinet_code="E99999",
        target_company_name="サンプル工業株式会社",
        target_sec_code="99990",
        submit_date_time="2026-02-18 10:00",
        pdf_flag=True,
        xbrl_flag=True,
    )
    db_session.add(tob)
    await db_session.commit()
    await db_session.refresh(tob)
    return tob


@pytest_asyncio.fixture
async def sample_watchlist_item(db_session):
    """Insert a sample watchlist item."""
    item = Watchlist(
        company_name="サンプル工業株式会社",
        sec_code="99990",
        edinet_code="E99999",
    )
    db_session.add(item)
    await db_session.commit()
    await db_session.refresh(item)
    return item


# Sample EDINET API response data
SAMPLE_EDINET_RESPONSE = {
    "metadata": {
        "title": "提出書類一覧及びメタデータ",
        "parameter": {"date": "2026-02-18", "type": "2"},
        "resultset": {"count": 2},
        "processDateTime": "2026-02-18 10:00",
        "status": "200",
        "message": "OK",
    },
    "results": [
        {
            "seqNumber": 1,
            "docID": "S100ABC1",
            "edinetCode": "E11111",
            "secCode": "11110",
            "JCN": "1234567890123",
            "filerName": "野村アセットマネジメント株式会社",
            "fundCode": None,
            "ordinanceCode": "060",
            "formCode": "010000",
            "docTypeCode": "350",
            "periodStart": None,
            "periodEnd": None,
            "submitDateTime": "2026-02-18 09:15",
            "docDescription": "大量保有報告書",
            "issuerEdinetCode": "E22222",
            "subjectEdinetCode": "E22222",
            "subsidiaryEdinetCode": None,
            "currentReportReason": None,
            "parentDocID": None,
            "opeDateTime": None,
            "withdrawalStatus": "0",
            "docInfoEditStatus": "0",
            "disclosureStatus": "0",
            "xbrlFlag": "1",
            "pdfFlag": "1",
            "attachDocFlag": "1",
            "englishDocFlag": "0",

            "legalStatus": "0",
        },
        {
            "seqNumber": 2,
            "docID": "S100ABC2",
            "edinetCode": "E33333",
            "secCode": None,
            "JCN": None,
            "filerName": "ブラックロック・ジャパン株式会社",
            "fundCode": None,
            "ordinanceCode": "060",
            "formCode": "010002",
            "docTypeCode": "350",
            "periodStart": None,
            "periodEnd": None,
            "submitDateTime": "2026-02-18 09:30",
            "docDescription": "変更報告書（特例対象株券等）",
            "issuerEdinetCode": "E44444",
            "subjectEdinetCode": "E44444",
            "subsidiaryEdinetCode": None,
            "currentReportReason": None,
            "parentDocID": None,
            "opeDateTime": None,
            "withdrawalStatus": "0",
            "docInfoEditStatus": "0",
            "disclosureStatus": "0",
            "xbrlFlag": "1",
            "pdfFlag": "1",
            "attachDocFlag": "0",
            "englishDocFlag": "0",

            "legalStatus": "0",
        },
        {
            "seqNumber": 3,
            "docID": "S100ABC3",
            "edinetCode": "E55555",
            "secCode": "55550",
            "JCN": None,
            "filerName": "テスト株式会社",
            "fundCode": None,
            "ordinanceCode": "010",
            "formCode": "030000",
            "docTypeCode": "120",
            "periodStart": "2025-04-01",
            "periodEnd": "2026-03-31",
            "submitDateTime": "2026-02-18 10:00",
            "docDescription": "有価証券報告書",
            "issuerEdinetCode": None,
            "subjectEdinetCode": None,
            "subsidiaryEdinetCode": None,
            "currentReportReason": None,
            "parentDocID": None,
            "opeDateTime": None,
            "withdrawalStatus": "0",
            "docInfoEditStatus": "0",
            "disclosureStatus": "0",
            "xbrlFlag": "1",
            "pdfFlag": "1",
            "attachDocFlag": "1",
            "englishDocFlag": "0",

            "legalStatus": "0",
        },
    ],
}
