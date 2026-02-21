"""Filing list and detail endpoints, including EDINET document proxy."""

import logging
from datetime import date

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse, Response
from sqlalchemy import desc, func, or_, select

from app.deps import get_async_session
from app.models import Filing

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/filings", tags=["Filings"])

# Separate router for document proxy (mounted at /api/documents)
documents_router = APIRouter(prefix="/api/documents", tags=["Documents"])


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
    async with get_async_session()() as session:
        query = select(Filing).order_by(desc(Filing.submit_date_time), desc(Filing.id))

        if date_from:
            query = query.where(
                Filing.submit_date_time >= date_from.isoformat()
            )
        if date_to:
            query = query.where(
                Filing.submit_date_time <= date_to.isoformat() + " 23:59:59"
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
    async with get_async_session()() as session:
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

def _make_pdf_response(content: bytes, doc_id: str) -> Response:
    return Response(
        content=content,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="{doc_id}.pdf"',
            "Cache-Control": "public, max-age=86400",
        },
    )


@documents_router.get("/{doc_id}/pdf")
async def proxy_document_pdf(doc_id: str) -> Response:
    """Proxy EDINET PDF download with multi-stage fallback.

    Retrieval order:
      1. EDINET API v2 (type=2) — requires Subscription-Key (server-side)
      2. disclosure2dl direct PDF — public, no auth needed
      3. Redirect to EDINET viewer website
    """
    import httpx as _httpx

    from app.config import settings
    from app.edinet import _looks_like_pdf, edinet_client

    # Sanitise doc_id to prevent path traversal
    if not doc_id.isalnum():
        return JSONResponse({"error": "Invalid document ID"}, status_code=400)

    # --- Stage 1: EDINET API v2 ---
    if settings.EDINET_API_KEY:
        content = await edinet_client.download_pdf(doc_id)
        if content:
            return _make_pdf_response(content, doc_id)
    else:
        logger.warning(
            "EDINET_API_KEY not configured — skipping API v2 PDF download"
        )

    # --- Stage 2: disclosure2dl direct PDF (public, no auth) ---
    dl_url = (
        "https://disclosure2dl.edinet-fsa.go.jp"
        f"/searchdocument/pdf/{doc_id}.pdf"
    )
    try:
        async with _httpx.AsyncClient(
            timeout=15.0, follow_redirects=True,
        ) as hc:
            resp = await hc.get(dl_url)
            if resp.status_code == 200 and _looks_like_pdf(resp.content):
                logger.info("Served %s via disclosure2dl fallback", doc_id)
                return _make_pdf_response(resp.content, doc_id)
            logger.info(
                "disclosure2dl returned %s for %s",
                resp.status_code, doc_id,
            )
    except Exception as e:
        logger.info("disclosure2dl request failed for %s: %s", doc_id, e)

    # --- Stage 3: Redirect to EDINET viewer ---
    viewer_url = (
        f"https://disclosure2.edinet-fsa.go.jp/WZEK0040.aspx"
        f"?{doc_id},,,"
    )
    logger.info(
        "PDF not downloadable for %s — redirecting to EDINET viewer",
        doc_id,
    )
    return JSONResponse(
        {
            "error": "PDF not available for direct download",
            "doc_id": doc_id,
            "redirect_url": viewer_url,
        },
        status_code=302,
        headers={"Location": viewer_url},
    )
