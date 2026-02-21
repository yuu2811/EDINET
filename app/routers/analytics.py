"""Rich analytics endpoints for the EDINET Large Shareholding Monitor."""

from datetime import date, timedelta

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from sqlalchemy import case, desc, distinct, func, or_, select

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


def _get_async_session():
    """Resolve async_session at runtime via app.main for testability."""
    import app.main
    return app.main.async_session


def _normalize_sec_code(raw: str | None) -> str | None:
    """Normalize a securities code to its 4-digit form.

    5-digit codes have a trailing check digit which is stripped.
    Returns None for None/empty input.
    """
    if not raw:
        return None
    code = raw.strip()
    if len(code) == 5:
        code = code[:4]
    return code


def _sec_code_to_sector(sec_code: str | None) -> str:
    """Map a securities code to its sector name.

    Uses the first 2 digits of the normalized 4-digit code.
    """
    norm = _normalize_sec_code(sec_code)
    if not norm or len(norm) < 2:
        return "その他"
    prefix = norm[:2]
    return _SECTOR_MAP.get(prefix, "その他")


def _ratio_change_expr():
    """SQLAlchemy expression for holding_ratio - previous_holding_ratio.

    Returns NULL when either value is NULL.
    """
    return case(
        (
            (Filing.holding_ratio.isnot(None)) & (Filing.previous_holding_ratio.isnot(None)),
            Filing.holding_ratio - Filing.previous_holding_ratio,
        ),
        else_=None,
    )


def _period_start_date(period: str) -> str | None:
    """Compute the start date string for a given period filter.

    Returns None for 'all' (no date filter).
    """
    today = date.today()
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
# 1. Filer Profile
# ---------------------------------------------------------------------------

@router.get("/filer/{edinet_code}")
async def filer_profile(edinet_code: str) -> dict:
    """Return a filer's full history and analytics."""
    async with _get_async_session()() as session:
        # Total filings for this filer
        total_result = await session.execute(
            select(func.count(Filing.id)).where(Filing.edinet_code == edinet_code)
        )
        total_filings = total_result.scalar() or 0

        if total_filings == 0:
            return JSONResponse(
                {"error": "No filings found for this filer"},
                status_code=404,
            )

        # Filer name from most recent filing
        name_result = await session.execute(
            select(Filing.filer_name)
            .where(Filing.edinet_code == edinet_code)
            .order_by(desc(Filing.submit_date_time))
            .limit(1)
        )
        filer_name = name_result.scalar()

        # Recent 20 filings
        recent_result = await session.execute(
            select(Filing)
            .where(Filing.edinet_code == edinet_code)
            .order_by(desc(Filing.submit_date_time), desc(Filing.id))
            .limit(20)
        )
        recent_filings = [f.to_dict() for f in recent_result.scalars().all()]

        # Target companies: aggregated by target_sec_code
        # Use a subquery with row_number to get the latest filing per target
        target_q = (
            select(
                Filing.target_company_name,
                Filing.target_sec_code,
                Filing.holding_ratio,
                func.count(Filing.id).over(
                    partition_by=func.coalesce(Filing.target_sec_code, Filing.target_company_name)
                ).label("filing_count"),
                func.row_number().over(
                    partition_by=func.coalesce(Filing.target_sec_code, Filing.target_company_name),
                    order_by=desc(Filing.submit_date_time),
                ).label("rn"),
            )
            .where(Filing.edinet_code == edinet_code)
            .where(or_(Filing.target_sec_code.isnot(None), Filing.target_company_name.isnot(None)))
        )
        target_sub = target_q.subquery()
        target_result = await session.execute(
            select(target_sub).where(target_sub.c.rn == 1)
        )
        target_companies = []
        for row in target_result:
            target_companies.append({
                "company_name": row.target_company_name,
                "sec_code": row.target_sec_code,
                "latest_ratio": row.holding_ratio,
                "filing_count": row.filing_count,
            })

        # Activity summary
        summary_result = await session.execute(
            select(
                func.min(Filing.submit_date_time).label("first_filing_date"),
                func.max(Filing.submit_date_time).label("last_filing_date"),
                func.avg(Filing.holding_ratio).label("avg_holding_ratio"),
            ).where(Filing.edinet_code == edinet_code)
        )
        summary_row = summary_result.one()
        avg_ratio = summary_row.avg_holding_ratio
        activity_summary = {
            "first_filing_date": summary_row.first_filing_date,
            "last_filing_date": summary_row.last_filing_date,
            "avg_holding_ratio": round(avg_ratio, 2) if avg_ratio is not None else None,
        }

        return {
            "filer_name": filer_name,
            "edinet_code": edinet_code,
            "total_filings": total_filings,
            "recent_filings": recent_filings,
            "target_companies": target_companies,
            "activity_summary": activity_summary,
        }


# ---------------------------------------------------------------------------
# 2. Target Company Profile
# ---------------------------------------------------------------------------

@router.get("/company/{sec_code}")
async def company_profile(sec_code: str) -> dict:
    """Return all filing analytics targeting a specific company."""
    async with _get_async_session()() as session:
        # Match on target_sec_code or sec_code (both the raw value and
        # common variants with/without trailing 0)
        code_variants = [sec_code]
        # 5-digit EDINET codes have a trailing check digit — strip it
        if len(sec_code) == 5:
            code_variants.append(sec_code[:4])
        if len(sec_code) == 4:
            code_variants.append(sec_code + "0")

        code_filter = or_(
            Filing.target_sec_code.in_(code_variants),
            Filing.sec_code.in_(code_variants),
        )

        # Total filings count
        total_result = await session.execute(
            select(func.count(Filing.id)).where(code_filter)
        )
        total_filings = total_result.scalar() or 0

        if total_filings == 0:
            return JSONResponse(
                {"error": "No filings found for this securities code"},
                status_code=404,
            )

        # Company name from latest filing
        name_result = await session.execute(
            select(Filing.target_company_name, Filing.target_sec_code)
            .where(code_filter)
            .order_by(desc(Filing.submit_date_time))
            .limit(1)
        )
        name_row = name_result.one()
        company_name = name_row.target_company_name
        normalized_sec = _normalize_sec_code(name_row.target_sec_code) or sec_code

        # Major holders: latest filing per filer
        holder_q = (
            select(
                Filing.filer_name,
                Filing.edinet_code,
                Filing.holding_ratio,
                Filing.submit_date_time,
                func.row_number().over(
                    partition_by=Filing.edinet_code,
                    order_by=desc(Filing.submit_date_time),
                ).label("rn"),
            )
            .where(code_filter)
            .where(Filing.edinet_code.isnot(None))
        )
        holder_sub = holder_q.subquery()
        holder_result = await session.execute(
            select(holder_sub)
            .where(holder_sub.c.rn == 1)
            .order_by(desc(holder_sub.c.holding_ratio))
        )
        major_holders = []
        for row in holder_result:
            major_holders.append({
                "filer_name": row.filer_name,
                "edinet_code": row.edinet_code,
                "latest_ratio": row.holding_ratio,
                "latest_date": row.submit_date_time,
            })

        # Recent 20 filings
        recent_result = await session.execute(
            select(Filing)
            .where(code_filter)
            .order_by(desc(Filing.submit_date_time), desc(Filing.id))
            .limit(20)
        )
        recent_filings = [f.to_dict() for f in recent_result.scalars().all()]

        # Holding history: chronological for charting
        history_result = await session.execute(
            select(
                Filing.submit_date_time,
                Filing.holding_ratio,
                Filing.filer_name,
            )
            .where(code_filter)
            .where(Filing.holding_ratio.isnot(None))
            .order_by(Filing.submit_date_time)
        )
        holding_history = []
        for row in history_result:
            holding_history.append({
                "date": row.submit_date_time,
                "ratio": row.holding_ratio,
                "filer_name": row.filer_name,
            })

        return {
            "company_name": company_name,
            "sec_code": normalized_sec,
            "total_filings": total_filings,
            "major_holders": major_holders,
            "recent_filings": recent_filings,
            "holding_history": holding_history,
        }


# ---------------------------------------------------------------------------
# 3. Activity Rankings
# ---------------------------------------------------------------------------

@router.get("/rankings")
async def activity_rankings(
    period: str = Query("30d", description="Period: 7d, 30d, 90d, all"),
) -> dict:
    """Return activity rankings for filers, companies, and ratio changes."""
    start_date = _period_start_date(period)

    async with _get_async_session()() as session:
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

        # Largest increases: top 10 with biggest positive ratio change
        ratio_change = _ratio_change_expr()
        increase_q = _period_filter(
            select(Filing)
            .where(Filing.holding_ratio.isnot(None))
            .where(Filing.previous_holding_ratio.isnot(None))
            .where(Filing.holding_ratio > Filing.previous_holding_ratio)
            .order_by(desc(Filing.holding_ratio - Filing.previous_holding_ratio))
            .limit(10)
        )
        increase_result = await session.execute(increase_q)
        largest_increases = [f.to_dict() for f in increase_result.scalars().all()]

        # Largest decreases: top 10 with biggest negative ratio change
        decrease_q = _period_filter(
            select(Filing)
            .where(Filing.holding_ratio.isnot(None))
            .where(Filing.previous_holding_ratio.isnot(None))
            .where(Filing.holding_ratio < Filing.previous_holding_ratio)
            .order_by(Filing.holding_ratio - Filing.previous_holding_ratio)
            .limit(10)
        )
        decrease_result = await session.execute(decrease_q)
        largest_decreases = [f.to_dict() for f in decrease_result.scalars().all()]

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
# 4. Market Movement Summary
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
            parsed = date.today()
    else:
        parsed = date.today()
    date_str = parsed.isoformat()

    async with _get_async_session()() as session:
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

        # Ratio change classification
        ratio_change = _ratio_change_expr()

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
# 5. Sector Breakdown
# ---------------------------------------------------------------------------

@router.get("/sectors")
async def sector_breakdown() -> dict:
    """Return sector-level aggregation of all filings in the database."""
    async with _get_async_session()() as session:
        # Fetch the minimal data needed for sector classification
        result = await session.execute(
            select(
                Filing.target_sec_code,
                Filing.holding_ratio,
            )
        )

        sector_data: dict[str, dict] = {}
        for row in result:
            sector = _sec_code_to_sector(row.target_sec_code)
            if sector not in sector_data:
                sector_data[sector] = {
                    "sec_codes": set(),
                    "filing_count": 0,
                    "ratios": [],
                }
            norm = _normalize_sec_code(row.target_sec_code)
            if norm:
                sector_data[sector]["sec_codes"].add(norm)
            sector_data[sector]["filing_count"] += 1
            if row.holding_ratio is not None:
                sector_data[sector]["ratios"].append(row.holding_ratio)

        sectors = []
        for sector, data in sorted(sector_data.items(), key=lambda x: -x[1]["filing_count"]):
            avg_ratio = None
            if data["ratios"]:
                avg_ratio = round(sum(data["ratios"]) / len(data["ratios"]), 2)
            sectors.append({
                "sector": sector,
                "company_count": len(data["sec_codes"]),
                "filing_count": data["filing_count"],
                "avg_ratio": avg_ratio,
            })

        return {"sectors": sectors}
