"""Background polling service for EDINET large shareholding filings."""

import asyncio
import json
import logging
import re
import time
from datetime import date

from sqlalchemy import func, select

from app.config import settings
from app.database import async_session
from app.demo_data import generate_demo_filings
from app.edinet import edinet_client
from app.models import CompanyInfo, Filing

logger = logging.getLogger(__name__)


class JsonEncoder(json.JSONEncoder):
    """JSON encoder that handles datetime and other types."""

    def default(self, obj):
        if hasattr(obj, "isoformat"):
            return obj.isoformat()
        return super().default(obj)


class SSEBroadcaster:
    """Manages SSE client connections and broadcasts events.

    Thread-safe broadcaster using asyncio.Lock with bounded queues,
    client ID tracking, and stale client cleanup.
    """

    _CLIENT_MAX_AGE = 3600  # 1 hour in seconds

    def __init__(self):
        self._lock = asyncio.Lock()
        self._clients: dict[int, tuple[asyncio.Queue, float]] = {}
        self._next_id: int = 0

    async def subscribe(self) -> tuple[int, asyncio.Queue]:
        """Register a new SSE client.

        Returns:
            A tuple of (client_id, queue) where the queue is bounded
            to 100 items.
        """
        async with self._lock:
            client_id = self._next_id
            self._next_id += 1
            q: asyncio.Queue = asyncio.Queue(maxsize=100)
            self._clients[client_id] = (q, time.monotonic())
            logger.debug("SSE client %d subscribed (total: %d)",
                         client_id, len(self._clients))
            return client_id, q

    async def unsubscribe(self, client_id: int) -> None:
        """Remove an SSE client by its ID."""
        async with self._lock:
            if client_id in self._clients:
                del self._clients[client_id]
                logger.debug("SSE client %d unsubscribed (total: %d)",
                             client_id, len(self._clients))

    async def broadcast(self, event: str, data: dict):
        """Broadcast an event to all connected SSE clients.

        If a client's queue is full, the client is dropped with a warning.
        """
        payload = json.dumps(data, cls=JsonEncoder, ensure_ascii=False)
        message = f"event: {event}\ndata: {payload}\n\n"
        dead: list[int] = []
        async with self._lock:
            for client_id, (q, _connected_at) in self._clients.items():
                try:
                    q.put_nowait(message)
                except asyncio.QueueFull:
                    logger.warning(
                        "SSE client %d queue full, dropping client",
                        client_id,
                    )
                    dead.append(client_id)
            for client_id in dead:
                del self._clients[client_id]

    async def _cleanup_stale(self) -> None:
        """Remove clients that have been connected longer than _CLIENT_MAX_AGE."""
        now = time.monotonic()
        async with self._lock:
            stale = [
                cid
                for cid, (_q, connected_at) in self._clients.items()
                if now - connected_at > self._CLIENT_MAX_AGE
            ]
            for cid in stale:
                logger.info("Removing stale SSE client %d", cid)
                del self._clients[cid]

    @property
    def client_count(self) -> int:
        return len(self._clients)


broadcaster = SSEBroadcaster()


async def poll_edinet(target_date=None):
    """Poll EDINET for new large shareholding filings."""
    today = target_date or date.today()

    logger.info("Polling EDINET for date %s...", today)

    # Retry with exponential backoff
    max_retries = 3
    delay = 2.0
    max_delay = 30.0
    filings = None
    for attempt in range(1, max_retries + 1):
        try:
            filings = await edinet_client.fetch_document_list(today)
            break
        except Exception as e:
            if attempt < max_retries:
                logger.warning(
                    "EDINET API call failed (attempt %d/%d): %s — retrying in %.1fs",
                    attempt, max_retries, e, delay,
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, max_delay)
            else:
                logger.error(
                    "EDINET API call failed after %d attempts: %s",
                    max_retries, e,
                    exc_info=True,
                )
                # Fall through to demo data check below
                filings = None

    if filings is None:
        filings = []

    if not filings:
        # If the DB is also empty, seed with demo data so the UI is functional
        await _seed_demo_if_empty(today)
        return

    new_count = 0
    async with async_session() as session:
        for doc in filings:
            doc_id = doc.get("docID")
            if not doc_id:
                continue

            # Check if already stored
            existing = await session.execute(
                select(Filing).where(Filing.doc_id == doc_id)
            )
            if existing.scalar_one_or_none():
                continue

            # New filing detected
            doc_description = doc.get("docDescription", "")
            is_amendment = doc.get("docTypeCode") == "360"
            is_special = "特例対象" in (doc_description or "")

            filing = Filing(
                doc_id=doc_id,
                seq_number=doc.get("seqNumber"),
                edinet_code=doc.get("edinetCode"),
                filer_name=doc.get("filerName"),
                sec_code=doc.get("secCode"),
                jcn=doc.get("JCN"),
                doc_type_code=doc.get("docTypeCode"),
                ordinance_code=doc.get("ordinanceCode"),
                form_code=doc.get("formCode"),
                doc_description=doc_description,
                subject_edinet_code=doc.get("subjectEdinetCode"),
                issuer_edinet_code=doc.get("issuerEdinetCode"),
                submit_date_time=doc.get("submitDateTime"),
                period_start=doc.get("periodStart"),
                period_end=doc.get("periodEnd"),
                # API v2: parentDocID (field #19)
                parent_doc_id=doc.get("parentDocID"),
                # API v2: status fields (string "0"/"1"/"2")
                withdrawal_status=doc.get("withdrawalStatus"),
                disclosure_status=doc.get("disclosureStatus"),
                doc_info_edit_status=doc.get("docInfoEditStatus"),
                # API v2: all flag fields are string "0"/"1"
                xbrl_flag=doc.get("xbrlFlag") == "1",
                pdf_flag=doc.get("pdfFlag") == "1",
                csv_flag=doc.get("csvFlag") == "1",  # API v2 new
                attach_doc_flag=doc.get("attachDocFlag") == "1",
                english_doc_flag=doc.get("englishDocFlag") == "1",
                is_amendment=is_amendment,
                is_special_exemption=is_special,
            )

            # --- Pre-enrichment from document list fields ---
            # secCode from the EDINET document list is the ISSUER's code
            # (= target company), so copy it to target_sec_code as well.
            if filing.sec_code and not filing.target_sec_code:
                filing.target_sec_code = filing.sec_code

            # Extract target company name from doc_description if available.
            # Typical format: "変更報告書（トヨタ自動車株式）"
            if not filing.target_company_name and doc_description:
                m = re.search(r"[（(]([^）)]+?)(?:株式|株券)[）)]", doc_description)
                if m:
                    filing.target_company_name = m.group(1)

            try:
                session.add(filing)
                await session.flush()

                # Try to enrich from XBRL/CSV for additional data
                if filing.xbrl_flag or filing.csv_flag:
                    # Per EDINET API v2 spec: several seconds delay between
                    # document downloads is recommended to avoid rate limiting.
                    if new_count > 0:
                        await asyncio.sleep(3.0)
                    await _enrich_from_xbrl(filing)

                await session.commit()
                await session.refresh(filing)
            except Exception as e:
                logger.error("Failed to store filing %s: %s", doc_id, e)
                await session.rollback()
                continue

            new_count += 1

            # Broadcast to SSE clients
            await broadcaster.broadcast("new_filing", filing.to_dict())
            logger.info(
                "New filing: %s - %s -> %s",
                filing.doc_id,
                filing.filer_name,
                filing.doc_description,
            )

    if new_count > 0:
        logger.info("Found %d new filings", new_count)
        await broadcaster.broadcast(
            "stats_update", {"new_count": new_count, "date": today.isoformat()}
        )


_demo_seeded = False  # only seed once per process lifetime


async def _seed_demo_if_empty(target_date: date) -> None:
    """Seed the database with demo filings when it's empty.

    This ensures the UI is fully functional even when the EDINET API
    and all external stock APIs are unreachable (e.g. sandboxed environments).
    Only runs once per process lifetime to avoid duplicates.
    """
    global _demo_seeded
    if _demo_seeded:
        return

    async with async_session() as session:
        count_result = await session.execute(select(func.count()).select_from(Filing))
        total = count_result.scalar() or 0
        if total > 0:
            _demo_seeded = True
            return

        logger.info("Database empty and EDINET API unreachable — seeding demo data")
        demo_filings = generate_demo_filings(target_date, count=25)
        for filing in demo_filings:
            session.add(filing)
        await session.commit()
        _demo_seeded = True
        logger.info("Seeded %d demo filings for %s", len(demo_filings), target_date)

        # Broadcast them to any connected SSE clients
        for filing in demo_filings:
            await session.refresh(filing)
            await broadcaster.broadcast("new_filing", filing.to_dict())

        await broadcaster.broadcast(
            "stats_update", {"new_count": len(demo_filings), "date": target_date.isoformat()}
        )


_XBRL_FIELDS = (
    "holding_ratio",
    "previous_holding_ratio",
    "holder_name",
    "target_company_name",
    "target_sec_code",
    "shares_held",
    "purpose_of_holding",
)


async def _enrich_from_xbrl(filing: Filing):
    """Download and parse XBRL/CSV to enrich filing data.

    Per EDINET API v2 spec, a delay of several seconds between document
    downloads is recommended to avoid rate limiting.  The caller is
    responsible for the inter-request delay; this function handles a
    single download.

    Strategy:
      1. Try XBRL (type=1) first — most detailed data
      2. If XBRL fails and CSV is available (csv_flag), try CSV (type=5)
    """
    data = None

    # --- Try XBRL (type=1) ---
    try:
        zip_content = await asyncio.wait_for(
            edinet_client.download_xbrl(filing.doc_id),
            timeout=30.0,
        )
        if zip_content:
            data = edinet_client.parse_xbrl_for_holding_data(zip_content)
    except asyncio.TimeoutError:
        logger.warning("XBRL download timed out for %s", filing.doc_id)
    except Exception as e:
        logger.error("XBRL download failed for %s: %s", filing.doc_id, e)

    # --- Fallback: try CSV (type=5, API v2 new feature) ---
    if data is None or data.get("holding_ratio") is None:
        if getattr(filing, "csv_flag", False):
            try:
                csv_content = await asyncio.wait_for(
                    edinet_client.download_csv(filing.doc_id),
                    timeout=30.0,
                )
                if csv_content:
                    csv_data = edinet_client.parse_csv_for_holding_data(csv_content)
                    # Merge: CSV fills in gaps that XBRL missed
                    if data is None:
                        data = csv_data
                    else:
                        for field in _XBRL_FIELDS:
                            if data.get(field) is None and csv_data.get(field) is not None:
                                data[field] = csv_data[field]
            except asyncio.TimeoutError:
                logger.warning("CSV download timed out for %s", filing.doc_id)
            except Exception as e:
                logger.debug("CSV download failed for %s: %s", filing.doc_id, e)

    if data is None:
        return

    for field in _XBRL_FIELDS:
        value = data.get(field)
        if value is not None:
            setattr(filing, field, value)

    filing.xbrl_parsed = True
    logger.info(
        "XBRL enriched %s: ratio=%.2f%%, target=%s",
        filing.doc_id,
        filing.holding_ratio or 0,
        filing.target_company_name or "N/A",
    )


_retry_offset = 0  # rotating offset for fair retry selection


async def _retry_xbrl_enrichment():
    """Retry XBRL enrichment for filings that haven't been parsed yet.

    Uses a rotating offset so different filings are attempted each cycle,
    preventing permanently-unparseable records from starving older ones.
    Each enrichment is bounded to 10s and the total batch to 30s to
    avoid stretching the polling interval.
    """
    global _retry_offset

    async with async_session() as session:
        total_unparsed_result = await session.execute(
            select(func.count()).select_from(
                select(Filing.id)
                .where(Filing.xbrl_flag.is_(True), Filing.xbrl_parsed.is_(False))
                .subquery()
            )
        )
        total_unparsed = total_unparsed_result.scalar() or 0
        if total_unparsed == 0:
            _retry_offset = 0
            return

        if _retry_offset >= total_unparsed:
            _retry_offset = 0

        result = await session.execute(
            select(Filing)
            .where(Filing.xbrl_flag.is_(True), Filing.xbrl_parsed.is_(False))
            .order_by(Filing.id.asc())
            .offset(_retry_offset)
            .limit(5)
        )
        filings = result.scalars().all()
        if not filings:
            _retry_offset = 0
            return

        _retry_offset += len(filings)

        logger.info(
            "Retrying XBRL enrichment for %d filings (offset=%d, unparsed=%d)",
            len(filings), _retry_offset - len(filings), total_unparsed,
        )

        async def _enrich_one(filing: Filing):
            try:
                await asyncio.wait_for(_enrich_from_xbrl(filing), timeout=10.0)
            except asyncio.TimeoutError:
                logger.warning("XBRL retry timed out for %s", filing.doc_id)

            if not filing.target_company_name and filing.doc_description:
                m = re.search(r"[（(]([^）)]+?)(?:株式|株券)[）)]", filing.doc_description)
                if m:
                    filing.target_company_name = m.group(1)
            if filing.sec_code and not filing.target_sec_code:
                filing.target_sec_code = filing.sec_code

        # Process sequentially with delay per EDINET API v2 spec recommendation
        # (several seconds between document downloads to avoid rate limiting)
        try:
            for i, f in enumerate(filings):
                if i > 0:
                    await asyncio.sleep(3.0)
                await asyncio.wait_for(_enrich_one(f), timeout=15.0)
        except asyncio.TimeoutError:
            logger.warning("XBRL retry batch timed out")

        await session.commit()


async def _poll_company_info(target_date: date) -> None:
    """Fetch 有報/四半期報告書 from EDINET and extract company fundamentals.

    Scans the daily document list for docTypeCodes 120/130/140 and
    downloads XBRL to extract 発行済株式数 and 純資産.  Results are
    stored in the CompanyInfo table (upserted by sec_code).
    """
    if not settings.EDINET_API_KEY:
        return

    all_docs = await edinet_client.fetch_all_document_list(target_date)
    if not all_docs:
        return

    target_docs = [
        doc for doc in all_docs
        if doc.get("docTypeCode") in settings.COMPANY_INFO_DOC_TYPES
        and doc.get("secCode")  # must have a securities code
        and doc.get("withdrawalStatus", "0") != "1"  # not withdrawn
        and doc.get("disclosureStatus", "0") == "0"  # disclosed
        and doc.get("xbrlFlag") == "1"  # has XBRL data
    ]

    if not target_docs:
        return

    logger.info(
        "Found %d company filings (有報/四半期) for %s",
        len(target_docs), target_date,
    )

    updated = 0
    async with async_session() as session:
        for i, doc in enumerate(target_docs):
            sec_code = doc["secCode"]
            doc_id = doc.get("docID")
            if not doc_id:
                continue

            # Rate-limit downloads per EDINET API v2 spec
            if i > 0:
                await asyncio.sleep(3.0)

            try:
                zip_content = await asyncio.wait_for(
                    edinet_client.download_xbrl(doc_id),
                    timeout=30.0,
                )
                if not zip_content:
                    continue

                info = edinet_client.parse_xbrl_for_company_info(zip_content)
                if not info.get("shares_outstanding") and not info.get("net_assets"):
                    continue

                # Normalise sec_code to 4 digits
                ticker = sec_code[:4] if len(sec_code) == 5 else sec_code

                # Use SAVEPOINT so a failure in one document doesn't
                # roll back previously flushed updates.
                async with session.begin_nested():
                    existing = await session.execute(
                        select(CompanyInfo).where(CompanyInfo.sec_code == ticker)
                    )
                    company = existing.scalar_one_or_none()
                    if company is None:
                        company = CompanyInfo(sec_code=ticker)
                        session.add(company)

                    company.edinet_code = doc.get("edinetCode")
                    if info.get("company_name"):
                        company.company_name = info["company_name"]
                    elif doc.get("filerName"):
                        company.company_name = doc["filerName"]
                    if info.get("shares_outstanding"):
                        company.shares_outstanding = info["shares_outstanding"]
                    if info.get("net_assets"):
                        company.net_assets = info["net_assets"]
                    company.source_doc_id = doc_id
                    company.source_doc_type = doc.get("docTypeCode")
                    company.period_end = doc.get("periodEnd")

                updated += 1

                logger.info(
                    "CompanyInfo updated: %s %s (shares=%s, net_assets=%s)",
                    ticker,
                    company.company_name,
                    company.shares_outstanding,
                    company.net_assets,
                )
            except asyncio.TimeoutError:
                logger.warning("Company info download timed out for %s", doc_id)
            except Exception as e:
                logger.error("Company info processing error for %s: %s", doc_id, e)
                continue

        if updated > 0:
            await session.commit()
            logger.info("Updated %d company info records from EDINET", updated)


async def run_poller():
    """Run the polling loop."""
    logger.info(
        "Starting EDINET poller (interval: %ds)...", settings.POLL_INTERVAL
    )
    while True:
        try:
            today = date.today()
            await poll_edinet(today)
            # Also retry enrichment for previously failed XBRL parses
            await _retry_xbrl_enrichment()
            # Fetch company fundamentals from 有報/四半期報告書
            await _poll_company_info(today)
        except asyncio.CancelledError:
            logger.info("Poller cancelled")
            raise
        except Exception as e:
            logger.error("Poller error: %s", e, exc_info=True)
        await asyncio.sleep(settings.POLL_INTERVAL)
