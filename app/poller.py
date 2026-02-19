"""Background polling service for EDINET large shareholding filings."""

import asyncio
import json
import logging
import time
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


async def poll_edinet():
    """Poll EDINET for new large shareholding filings."""
    today = date.today()

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
                return

    if filings is None:
        filings = []

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

            try:
                session.add(filing)
                await session.flush()

                # Try to parse XBRL for additional data
                if filing.xbrl_flag and settings.EDINET_API_KEY:
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
    """Download and parse XBRL to enrich filing data."""
    try:
        zip_content = await asyncio.wait_for(
            edinet_client.download_xbrl(filing.doc_id),
            timeout=30.0,
        )
        if not zip_content:
            return

        data = edinet_client.parse_xbrl_for_holding_data(zip_content)

        for field in _XBRL_FIELDS:
            value = data[field]
            if value is not None:
                setattr(filing, field, value)

        filing.xbrl_parsed = True
        logger.info(
            "XBRL enriched %s: ratio=%.2f%%, target=%s",
            filing.doc_id,
            filing.holding_ratio or 0,
            filing.target_company_name or "N/A",
        )
    except asyncio.TimeoutError:
        logger.warning("XBRL download timed out for %s", filing.doc_id)
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
        except asyncio.CancelledError:
            logger.info("Poller cancelled")
            raise
        except Exception as e:
            logger.error("Poller error: %s", e, exc_info=True)
        await asyncio.sleep(settings.POLL_INTERVAL)
