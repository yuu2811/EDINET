"""Filing list and detail endpoints, including EDINET document proxy."""

import logging
from datetime import date

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse, Response
from sqlalchemy import desc, func, or_, select

from app.models import Filing

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/filings", tags=["Filings"])

# Separate router for document proxy (mounted at /api/documents)
documents_router = APIRouter(prefix="/api/documents", tags=["Documents"])


def _get_async_session():
    """Resolve async_session at runtime via app.main for testability."""
    import app.main
    return app.main.async_session


@router.get("")
async def list_filings(
    date_from: date | None = Query(None, description="Start date (YYYY-MM-DD)"),
    date_to: date | None = Query(None, description="End date (YYYY-MM-DD)"),
    filer: str | None = Query(None, description="Filer name search"),
    target: str | None = Query(None, description="Target company name search"),
    sec_code: str | None = Query(None, description="Securities code"),
    amendment_only: bool = Query(False, description="Show only amendments"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict:
    """List large shareholding filings with filters."""
    async with _get_async_session()() as session:
        query = select(Filing).order_by(desc(Filing.submit_date_time), desc(Filing.id))

        if date_from:
            query = query.where(
                Filing.submit_date_time >= date_from.isoformat()
            )
        if date_to:
            query = query.where(
                Filing.submit_date_time <= date_to.isoformat() + " 23:59"
            )
        if filer:
            query = query.where(
                or_(
                    Filing.filer_name.contains(filer),
                    Filing.holder_name.contains(filer),
                )
            )
        if target:
            query = query.where(
                or_(
                    Filing.target_company_name.contains(target),
                    Filing.doc_description.contains(target),
                )
            )
        if sec_code:
            query = query.where(
                or_(
                    Filing.sec_code == sec_code,
                    Filing.target_sec_code == sec_code,
                )
            )
        if amendment_only:
            query = query.where(Filing.is_amendment.is_(True))

        count_query = select(func.count()).select_from(query.subquery())
        total = (await session.execute(count_query)).scalar()

        result = await session.execute(query.offset(offset).limit(limit))
        filings = result.scalars().all()

        return {
            "total": total,
            "offset": offset,
            "limit": limit,
            "filings": [f.to_dict() for f in filings],
        }


@router.get("/{doc_id}")
async def get_filing(doc_id: str) -> dict:
    """Get a single filing by document ID."""
    async with _get_async_session()() as session:
        result = await session.execute(
            select(Filing).where(Filing.doc_id == doc_id)
        )
        filing = result.scalar_one_or_none()
        if not filing:
            return JSONResponse({"error": "Filing not found"}, status_code=404)
        return filing.to_dict()


# ---------------------------------------------------------------------------
# Document proxy — EDINET API v2 requires Subscription-Key which must not
# be exposed to browser clients.  These endpoints act as a server-side proxy.
# ---------------------------------------------------------------------------

@documents_router.get("/{doc_id}/pdf")
async def proxy_document_pdf(doc_id: str) -> Response:
    """Proxy EDINET PDF download (type=2).

    Per EDINET API v2 spec, browser-based JavaScript cannot call the
    EDINET API directly, and the Subscription-Key must stay server-side.
    """
    from app.config import settings
    from app.edinet import edinet_client

    # Sanitise doc_id to prevent path traversal
    if not doc_id.isalnum():
        return JSONResponse({"error": "Invalid document ID"}, status_code=400)

    if not settings.EDINET_API_KEY:
        # No API key configured — redirect to EDINET disclosure page instead
        edinet_url = f"https://disclosure2.edinet-fsa.go.jp/WZEK0040.aspx?{doc_id},0,0="
        return Response(
            status_code=302,
            headers={"Location": edinet_url},
        )

    content = await edinet_client.download_pdf(doc_id)
    if content is None:
        logger.warning("PDF download failed for %s, redirecting to EDINET", doc_id)
        edinet_url = f"https://disclosure2.edinet-fsa.go.jp/WZEK0040.aspx?{doc_id},0,0="
        return Response(
            status_code=302,
            headers={"Location": edinet_url},
        )

    # Verify the response looks like a PDF (starts with %PDF)
    if len(content) < 5 or not content[:5].startswith(b"%PDF"):
        logger.warning("EDINET returned non-PDF content for %s (%d bytes)", doc_id, len(content))
        edinet_url = f"https://disclosure2.edinet-fsa.go.jp/WZEK0040.aspx?{doc_id},0,0="
        return Response(
            status_code=302,
            headers={"Location": edinet_url},
        )

    return Response(
        content=content,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="{doc_id}.pdf"',
            "Cache-Control": "public, max-age=86400",
        },
    )
