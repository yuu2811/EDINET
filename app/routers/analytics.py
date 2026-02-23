"""Rich analytics endpoints for the EDINET Large Shareholding Monitor."""

from datetime import date, datetime, timedelta

from fastapi import APIRouter, HTTPException, Path, Query
from sqlalchemy import case, desc, func, select

from app.config import JST
from app.deps import get_async_session, normalize_sec_code, validate_edinet_code, validate_sec_code
from app.models import Filing

router = APIRouter(prefix="/api/analytics", tags=["Analytics"])

_SECTOR_MAP = {
    "13": "水産・農林", "15": "鉱業", "17": "建設",
    "21": "食料品", "22": "繊維", "23": "パルプ・紙",
    "24": "化学", "25": "医薬品", "26": "石油・石炭",
    "27": "ゴム", "28": "ガラス・土石", "29": "鉄鋼",
    "31": "非鉄金属", "32": "金属製品", "33": "機械",
    "34": "電気機器", "35": "輸送用機器", "36": "精密機器",
    "37": "その他製品", "39": "電気・ガス", "40": "陸運",
    "41": "海運", "42": "空運", "43": "倉庫・運輸関連",
    "44": "情報・通信", "45": "卸売", "46": "小売",
    "47": "銀行", "48": "証券・商品先物", "49": "保険",
    "51": "その他金融", "52": "不動産", "53": "サービス",
    "69": "半導体・電子部品",
}


def _sec_code_to_sector(sec_code: str | None) -> str:
    """Map a securities code to its sector name."""
    norm = normalize_sec_code(sec_code)
    if not norm or len(norm) < 2:
        return "その他"
    return _SECTOR_MAP.get(norm[:2], "その他")


_VALID_PERIODS = {"7d", "30d", "90d", "all"}


def _period_start_date(period: str) -> str | None:
    """Compute the start date string for a given period filter.

    Returns None for 'all' (no date filter).
    """
    if period not in _VALID_PERIODS:
        period = "30d"
    today = datetime.now(JST).date()
    if period == "7d":
        start = today - timedelta(days=7)
    elif period == "90d":
        start = today - timedelta(days=90)
    elif period == "all":
        return None
    else:
        # Default: 30d
        start = today - timedelta(days=30)
    return start.isoformat()


# ---------------------------------------------------------------------------
# Activity Rankings
# ---------------------------------------------------------------------------

@router.get("/rankings")
async def activity_rankings(
    period: str = Query("30d", description="Period: 7d, 30d, 90d, all"),
) -> dict:
    """Return activity rankings for filers, companies, and ratio changes."""
    start_date = _period_start_date(period)

    async with get_async_session()() as session:
        # Base filter for the time period
        def _period_filter(stmt):
            if start_date is not None:
                return stmt.where(Filing.submit_date_time >= start_date)
            return stmt

        # Most active filers: top 10 by filing count
        filer_q = _period_filter(
            select(
                Filing.filer_name,
                Filing.edinet_code,
                func.count(Filing.id).label("filing_count"),
            )
            .where(Filing.filer_name.isnot(None))
            .group_by(Filing.filer_name, Filing.edinet_code)
            .order_by(desc("filing_count"))
            .limit(10)
        )
        filer_result = await session.execute(filer_q)
        most_active_filers = [
            {"filer_name": r.filer_name, "edinet_code": r.edinet_code, "filing_count": r.filing_count}
            for r in filer_result
        ]

        # Most targeted companies: top 10 by filing count
        target_q = _period_filter(
            select(
                Filing.target_company_name,
                Filing.target_sec_code,
                func.count(Filing.id).label("filing_count"),
            )
            .where(Filing.target_company_name.isnot(None))
            .group_by(Filing.target_company_name, Filing.target_sec_code)
            .order_by(desc("filing_count"))
            .limit(10)
        )
        target_result = await session.execute(target_q)
        most_targeted_companies = [
            {
                "company_name": r.target_company_name,
                "sec_code": r.target_sec_code,
                "filing_count": r.filing_count,
            }
            for r in target_result
        ]

        # Largest increases / decreases
        ratio_diff = Filing.holding_ratio - Filing.previous_holding_ratio
        ratio_base = (
            select(Filing)
            .where(Filing.holding_ratio.isnot(None))
            .where(Filing.previous_holding_ratio.isnot(None))
        )
        inc_q = _period_filter(
            ratio_base.where(Filing.holding_ratio > Filing.previous_holding_ratio)
            .order_by(desc(ratio_diff)).limit(10)
        )
        dec_q = _period_filter(
            ratio_base.where(Filing.holding_ratio < Filing.previous_holding_ratio)
            .order_by(ratio_diff).limit(10)
        )
        largest_increases = [f.to_dict() for f in (await session.execute(inc_q)).scalars().all()]
        largest_decreases = [f.to_dict() for f in (await session.execute(dec_q)).scalars().all()]

        # Busiest days: top 5 by filing count
        # Extract the date part from submit_date_time (first 10 chars = "YYYY-MM-DD")
        date_part = func.substr(Filing.submit_date_time, 1, 10)
        day_q = _period_filter(
            select(
                date_part.label("filing_date"),
                func.count(Filing.id).label("filing_count"),
            )
            .where(Filing.submit_date_time.isnot(None))
            .group_by(date_part)
            .order_by(desc("filing_count"))
            .limit(5)
        )
        day_result = await session.execute(day_q)
        busiest_days = [
            {"date": r.filing_date, "filing_count": r.filing_count}
            for r in day_result
        ]

        return {
            "period": period,
            "most_active_filers": most_active_filers,
            "most_targeted_companies": most_targeted_companies,
            "largest_increases": largest_increases,
            "largest_decreases": largest_decreases,
            "busiest_days": busiest_days,
        }


# ---------------------------------------------------------------------------
# Market Movement Summary
# ---------------------------------------------------------------------------

@router.get("/movements")
async def market_movements(
    target_date: str | None = Query(None, alias="date", description="Date (YYYY-MM-DD)"),
) -> dict:
    """Return a market movement summary for a given date."""
    if target_date:
        try:
            parsed = date.fromisoformat(target_date)
        except ValueError:
            parsed = datetime.now(JST).date()
    else:
        parsed = datetime.now(JST).date()
    date_str = parsed.isoformat()

    async with get_async_session()() as session:
        date_filter = Filing.submit_date_time.startswith(date_str)

        # Total filings on the date
        total_result = await session.execute(
            select(func.count(Filing.id)).where(date_filter)
        )
        total_filings = total_result.scalar() or 0

        if total_filings == 0:
            return {
                "date": date_str,
                "total_filings": 0,
                "net_direction": "neutral",
                "increases": 0,
                "decreases": 0,
                "unchanged": 0,
                "avg_increase": None,
                "avg_decrease": None,
                "sector_movements": [],
                "notable_moves": [],
            }

        # Counts by direction
        increase_count_result = await session.execute(
            select(func.count(Filing.id)).where(
                date_filter,
                Filing.holding_ratio.isnot(None),
                Filing.previous_holding_ratio.isnot(None),
                Filing.holding_ratio > Filing.previous_holding_ratio,
            )
        )
        increases = increase_count_result.scalar() or 0

        decrease_count_result = await session.execute(
            select(func.count(Filing.id)).where(
                date_filter,
                Filing.holding_ratio.isnot(None),
                Filing.previous_holding_ratio.isnot(None),
                Filing.holding_ratio < Filing.previous_holding_ratio,
            )
        )
        decreases = decrease_count_result.scalar() or 0

        unchanged = total_filings - increases - decreases

        # Net direction
        if increases > decreases:
            net_direction = "bullish"
        elif decreases > increases:
            net_direction = "bearish"
        else:
            net_direction = "neutral"

        # Average increase
        avg_inc_result = await session.execute(
            select(
                func.avg(Filing.holding_ratio - Filing.previous_holding_ratio)
            ).where(
                date_filter,
                Filing.holding_ratio.isnot(None),
                Filing.previous_holding_ratio.isnot(None),
                Filing.holding_ratio > Filing.previous_holding_ratio,
            )
        )
        avg_increase_raw = avg_inc_result.scalar()
        avg_increase = round(avg_increase_raw, 2) if avg_increase_raw is not None else None

        # Average decrease
        avg_dec_result = await session.execute(
            select(
                func.avg(Filing.holding_ratio - Filing.previous_holding_ratio)
            ).where(
                date_filter,
                Filing.holding_ratio.isnot(None),
                Filing.previous_holding_ratio.isnot(None),
                Filing.holding_ratio < Filing.previous_holding_ratio,
            )
        )
        avg_decrease_raw = avg_dec_result.scalar()
        avg_decrease = round(avg_decrease_raw, 2) if avg_decrease_raw is not None else None

        # Sector movements — must be computed in Python since sector mapping
        # is application-level logic, not stored in DB
        all_filings_result = await session.execute(
            select(
                Filing.target_sec_code,
                Filing.holding_ratio,
                Filing.previous_holding_ratio,
            ).where(date_filter)
        )
        sector_data: dict[str, dict] = {}
        for row in all_filings_result:
            sector = _sec_code_to_sector(row.target_sec_code)
            if sector not in sector_data:
                sector_data[sector] = {"count": 0, "changes": []}
            sector_data[sector]["count"] += 1
            if row.holding_ratio is not None and row.previous_holding_ratio is not None:
                sector_data[sector]["changes"].append(
                    row.holding_ratio - row.previous_holding_ratio
                )

        sector_movements = []
        for sector, data in sorted(sector_data.items(), key=lambda x: -x[1]["count"]):
            avg_change = None
            if data["changes"]:
                avg_change = round(sum(data["changes"]) / len(data["changes"]), 2)
            sector_movements.append({
                "sector": sector,
                "count": data["count"],
                "avg_change": avg_change,
            })

        # Notable moves: top 5 filings by absolute ratio change
        notable_q = (
            select(Filing)
            .where(
                date_filter,
                Filing.holding_ratio.isnot(None),
                Filing.previous_holding_ratio.isnot(None),
            )
            .order_by(desc(func.abs(Filing.holding_ratio - Filing.previous_holding_ratio)))
            .limit(5)
        )
        notable_result = await session.execute(notable_q)
        notable_moves = [f.to_dict() for f in notable_result.scalars().all()]

        return {
            "date": date_str,
            "total_filings": total_filings,
            "net_direction": net_direction,
            "increases": increases,
            "decreases": decreases,
            "unchanged": unchanged,
            "avg_increase": avg_increase,
            "avg_decrease": avg_decrease,
            "sector_movements": sector_movements,
            "notable_moves": notable_moves,
        }


# ---------------------------------------------------------------------------
# Sector Breakdown
# ---------------------------------------------------------------------------

@router.get("/sectors")
async def sector_breakdown() -> dict:
    """Return sector-level aggregation of all filings in the database.

    Uses SQL GROUP BY on the sector prefix (first 2 digits of securities
    code) to avoid loading all rows into Python memory.
    """
    async with get_async_session()() as session:
        # Derive the 4-digit ticker, then take the first 2 digits as sector prefix
        ticker_expr = case(
            (func.length(Filing.target_sec_code) == 5,
             func.substr(Filing.target_sec_code, 1, 4)),
            else_=Filing.target_sec_code,
        )
        sector_prefix = func.substr(ticker_expr, 1, 2)

        result = await session.execute(
            select(
                sector_prefix.label("prefix"),
                func.count(Filing.id).label("filing_count"),
                func.count(func.distinct(ticker_expr)).label("company_count"),
                func.avg(Filing.holding_ratio).label("avg_ratio"),
            )
            .where(Filing.target_sec_code.isnot(None))
            .group_by(sector_prefix)
        )

        # Also count filings with no sec_code (→ "その他")
        null_result = await session.execute(
            select(
                func.count(Filing.id).label("filing_count"),
                func.avg(Filing.holding_ratio).label("avg_ratio"),
            )
            .where(Filing.target_sec_code.is_(None))
        )
        null_row = null_result.one()

        sectors = []
        for row in result:
            sector_name = _SECTOR_MAP.get(row.prefix, "その他")
            avg_ratio = round(row.avg_ratio, 2) if row.avg_ratio is not None else None
            sectors.append({
                "sector": sector_name,
                "company_count": row.company_count,
                "filing_count": row.filing_count,
                "avg_ratio": avg_ratio,
            })

        # Merge null sec_code filings into "その他"
        if null_row.filing_count > 0:
            other = next((s for s in sectors if s["sector"] == "その他"), None)
            if other:
                other["filing_count"] += null_row.filing_count
            else:
                avg_ratio = round(null_row.avg_ratio, 2) if null_row.avg_ratio is not None else None
                sectors.append({
                    "sector": "その他",
                    "company_count": 0,
                    "filing_count": null_row.filing_count,
                    "avg_ratio": avg_ratio,
                })

        # Sort by filing count descending
        sectors.sort(key=lambda s: -s["filing_count"])

        return {"sectors": sectors}


# ---------------------------------------------------------------------------
# Shared profile helpers
# ---------------------------------------------------------------------------

def _group_filings(filings, key_fn, init_fn):
    """Group filings by key, building a dict of {key: {init_fields + filing_count + history}}."""
    groups: dict[str, dict] = {}
    for f in filings:
        key = key_fn(f)
        if key not in groups:
            groups[key] = {**init_fn(f), "filing_count": 0, "history": []}
        groups[key]["filing_count"] += 1
        if f.holding_ratio is not None:
            groups[key]["history"].append({
                "date": f.submit_date_time,
                "ratio": f.holding_ratio,
                "previous_ratio": f.previous_holding_ratio,
            })
    return groups


async def _profile_query(session, where_clause, limit, offset):
    """Execute count + paginated filing query for profile endpoints."""
    total_count = (await session.execute(
        select(func.count(Filing.id)).where(where_clause)
    )).scalar() or 0
    filings = (await session.execute(
        select(Filing).where(where_clause)
        .order_by(desc(Filing.submit_date_time))
        .offset(offset).limit(limit)
    )).scalars().all()
    return total_count, filings


# ---------------------------------------------------------------------------
# Filer Profile
# ---------------------------------------------------------------------------

@router.get("/filer/{edinet_code}")
async def filer_profile(
    edinet_code: str = Path(..., description="Filer EDINET code (e.g. E12345)"),
    limit: int = Query(200, ge=1, le=1000, description="Max filings to fetch"),
    offset: int = Query(0, ge=0, description="Offset for pagination"),
) -> dict:
    """Return a filer's full history, target companies, and activity summary."""
    edinet_code = validate_edinet_code(edinet_code)
    async with get_async_session()() as session:
        total_count, filings = await _profile_query(
            session, Filing.edinet_code == edinet_code, limit, offset,
        )
        if not filings:
            raise HTTPException(status_code=404, detail="Filer not found")

        targets = _group_filings(
            filings,
            key_fn=lambda f: f.target_sec_code or f.target_company_name or f.doc_id,
            init_fn=lambda f: {
                "company_name": f.target_company_name,
                "sec_code": f.target_sec_code,
                "latest_ratio": f.holding_ratio,
                "latest_date": f.submit_date_time,
            },
        )

        ratios = [f.holding_ratio for f in filings if f.holding_ratio is not None]
        dates = [f.submit_date_time for f in filings if f.submit_date_time]

        return {
            "edinet_code": edinet_code,
            "filer_name": filings[0].filer_name or filings[0].holder_name or edinet_code,
            "summary": {
                "total_filings": total_count,
                "fetched_filings": len(filings),
                "unique_targets": len(targets),
                "avg_holding_ratio": round(sum(ratios) / len(ratios), 2) if ratios else None,
                "first_filing": min(dates) if dates else None,
                "last_filing": max(dates) if dates else None,
            },
            "has_more": offset + len(filings) < total_count,
            "targets": sorted(targets.values(), key=lambda t: -t["filing_count"]),
            "recent_filings": [f.to_dict() for f in filings[:20]],
        }


# ---------------------------------------------------------------------------
# Company Profile
# ---------------------------------------------------------------------------

@router.get("/company/{sec_code}")
async def company_profile(
    sec_code: str = Path(..., description="Securities code (4 or 5 digit)"),
    limit: int = Query(200, ge=1, le=1000, description="Max filings to fetch"),
    offset: int = Query(0, ge=0, description="Offset for pagination"),
) -> dict:
    """Return all large shareholding data for a specific company."""
    normalized = validate_sec_code(sec_code)
    codes = [normalized, normalized + "0"]
    where = (Filing.target_sec_code.in_(codes)) | (Filing.sec_code.in_(codes))

    async with get_async_session()() as session:
        total_count, filings = await _profile_query(session, where, limit, offset)
        if not filings:
            raise HTTPException(status_code=404, detail="Company not found")

        company_name = next((f.target_company_name for f in filings if f.target_company_name), None)

        holders = _group_filings(
            filings,
            key_fn=lambda f: f.edinet_code or f.filer_name or f.doc_id,
            init_fn=lambda f: {
                "filer_name": f.holder_name or f.filer_name,
                "edinet_code": f.edinet_code,
                "latest_ratio": f.holding_ratio,
                "latest_date": f.submit_date_time,
            },
        )

        return {
            "sec_code": normalized,
            "company_name": company_name,
            "sector": _sec_code_to_sector(normalized),
            "holder_count": len(holders),
            "total_filings": total_count,
            "fetched_filings": len(filings),
            "has_more": offset + len(filings) < total_count,
            "holders": sorted(
                holders.values(),
                key=lambda h: h["latest_ratio"] if h["latest_ratio"] is not None else -1,
                reverse=True,
            ),
            "recent_filings": [f.to_dict() for f in filings[:20]],
        }
