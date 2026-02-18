"""Background polling service for EDINET large shareholding filings."""

import asyncio
import json
import logging
from datetime import date

from sqlalchemy import select

from app.config import settings
from app.database import async_session
from app.edinet import edinet_client
from app.models import Filing

logger = logging.getLogger(__name__)


class JsonEncoder(json.JSONEncoder):
    """JSON encoder that handles datetime and other types."""

    def default(self, obj):
        if hasattr(obj, "isoformat"):
            return obj.isoformat()
        return super().default(obj)


class SSEBroadcaster:
    """Manages SSE client connections and broadcasts events."""

    def __init__(self):
        self._queues: list[asyncio.Queue] = []

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._queues.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue):
        self._queues.remove(q)

    async def broadcast(self, event: str, data: dict):
        payload = json.dumps(data, cls=JsonEncoder, ensure_ascii=False)
        message = f"event: {event}\ndata: {payload}\n\n"
        dead: list[asyncio.Queue] = []
        for q in self._queues:
            try:
                q.put_nowait(message)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            self._queues.remove(q)

    @property
    def client_count(self) -> int:
        return len(self._queues)


broadcaster = SSEBroadcaster()


async def poll_edinet():
    """Poll EDINET for new large shareholding filings."""
    today = date.today()

    logger.info("Polling EDINET for date %s...", today)

    filings = await edinet_client.fetch_document_list(today)

    if not filings:
        logger.info("No large shareholding filings found for %s", today)
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
                xbrl_flag=doc.get("xbrlFlag") == "1",
                pdf_flag=doc.get("pdfFlag") == "1",
                is_amendment=is_amendment,
                is_special_exemption=is_special,
            )

            session.add(filing)
            await session.flush()

            # Try to parse XBRL for additional data
            if filing.xbrl_flag and settings.EDINET_API_KEY:
                await _enrich_from_xbrl(filing)

            await session.commit()
            await session.refresh(filing)

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


async def _enrich_from_xbrl(filing: Filing):
    """Download and parse XBRL to enrich filing data."""
    try:
        zip_content = await edinet_client.download_xbrl(filing.doc_id)
        if not zip_content:
            return

        data = edinet_client.parse_xbrl_for_holding_data(zip_content)

        if data["holding_ratio"] is not None:
            filing.holding_ratio = data["holding_ratio"]
        if data["previous_holding_ratio"] is not None:
            filing.previous_holding_ratio = data["previous_holding_ratio"]
        if data["holder_name"]:
            filing.holder_name = data["holder_name"]
        if data["target_company_name"]:
            filing.target_company_name = data["target_company_name"]
        if data["target_sec_code"]:
            filing.target_sec_code = data["target_sec_code"]
        if data["shares_held"] is not None:
            filing.shares_held = data["shares_held"]
        if data["purpose_of_holding"]:
            filing.purpose_of_holding = data["purpose_of_holding"]

        filing.xbrl_parsed = True
        logger.info(
            "XBRL enriched %s: ratio=%.2f%%, target=%s",
            filing.doc_id,
            filing.holding_ratio or 0,
            filing.target_company_name or "N/A",
        )
    except Exception as e:
        logger.error("Failed to enrich filing %s from XBRL: %s", filing.doc_id, e)


async def run_poller():
    """Run the polling loop."""
    logger.info(
        "Starting EDINET poller (interval: %ds)...", settings.POLL_INTERVAL
    )
    while True:
        try:
            await poll_edinet()
        except Exception as e:
            logger.error("Poller error: %s", e)
        await asyncio.sleep(settings.POLL_INTERVAL)
