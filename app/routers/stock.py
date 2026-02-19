"""Stock price data endpoint using external free APIs."""

import asyncio
import csv
import io
import logging
import time
from datetime import date, timedelta

import httpx
from fastapi import APIRouter, HTTPException

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/stock", tags=["Stock"])

# ---------------------------------------------------------------------------
# In-memory cache with TTL
# ---------------------------------------------------------------------------
_cache: dict[str, tuple[float, dict]] = {}
_CACHE_TTL = 30 * 60  # 30 minutes


def _cache_get(key: str) -> dict | None:
    """Return cached value if present and not expired."""
    entry = _cache.get(key)
    if entry is None:
        return None
    ts, value = entry
    if time.monotonic() - ts > _CACHE_TTL:
        del _cache[key]
        return None
    return value


def _cache_set(key: str, value: dict) -> None:
    _cache[key] = (time.monotonic(), value)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _normalise_sec_code(sec_code: str) -> str:
    """Convert EDINET securities code to a 4-digit ticker.

    EDINET codes are typically 5 digits (e.g. "39320").  Strip the trailing
    check-digit "0" to get the 4-digit TSE ticker (e.g. "3932").
    If the code is already 4 digits, return as-is.
    """
    sec_code = sec_code.strip()
    if len(sec_code) == 5 and sec_code[-1] == "0":
        return sec_code[:4]
    if len(sec_code) == 4 and sec_code.isdigit():
        return sec_code
    # Fallback: return as-is
    return sec_code


def _format_market_cap(value: float | int | None) -> str | None:
    """Format a market cap (JPY) in Japanese style using 億 / 兆."""
    if value is None:
        return None
    value = float(value)
    cho = 1_000_000_000_000  # 兆 = 1 trillion
    oku = 100_000_000  # 億 = 100 million

    if value >= cho:
        whole = int(value // cho)
        remainder_oku = int((value % cho) // oku)
        if remainder_oku:
            return f"{whole}兆{remainder_oku}億"
        return f"{whole}兆"
    if value >= oku:
        oku_val = int(value // oku)
        return f"{oku_val}億"
    # Smaller than 1億 – just show raw number
    return f"{int(value)}"


def _parse_float(v: str | None) -> float | None:
    if v is None:
        return None
    v = v.strip()
    if not v or v in ("N/A", "-", ""):
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def _parse_int(v: str | None) -> int | None:
    f = _parse_float(v)
    if f is None:
        return None
    return int(f)


# ---------------------------------------------------------------------------
# External API fetchers
# ---------------------------------------------------------------------------

async def _fetch_stooq_history(
    client: httpx.AsyncClient,
    ticker: str,
) -> list[dict]:
    """Fetch weekly price history from stooq.com CSV API.

    Returns list of dicts with keys: date, open, high, low, close, volume.
    """
    end = date.today()
    start = end - timedelta(days=3 * 365)
    d1 = start.strftime("%Y%m%d")
    d2 = end.strftime("%Y%m%d")

    url = (
        f"https://stooq.com/q/d/l/?s={ticker}.JP"
        f"&d1={d1}&d2={d2}&i=w"
    )

    try:
        resp = await client.get(url)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        logger.warning("stooq history request failed for %s: %s", ticker, exc)
        return []

    text = resp.text.strip()
    if not text or "No data" in text:
        return []

    reader = csv.DictReader(io.StringIO(text))
    rows: list[dict] = []
    for row in reader:
        entry: dict = {}
        entry["date"] = row.get("Date", "")
        entry["open"] = _parse_float(row.get("Open"))
        entry["high"] = _parse_float(row.get("High"))
        entry["low"] = _parse_float(row.get("Low"))
        entry["close"] = _parse_float(row.get("Close"))
        entry["volume"] = _parse_int(row.get("Volume"))
        if entry["date"]:
            rows.append(entry)
    return rows


async def _fetch_stooq_quote(
    client: httpx.AsyncClient,
    ticker: str,
) -> dict:
    """Fetch current quote from stooq.com CSV API.

    Returns dict with keys: name, close (current price).
    """
    url = (
        f"https://stooq.com/q/l/?s={ticker}.JP"
        f"&f=sd2t2ohlcvn&e=csv"
    )

    result: dict = {"name": None, "current_price": None}
    try:
        resp = await client.get(url)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        logger.warning("stooq quote request failed for %s: %s", ticker, exc)
        return result

    text = resp.text.strip()
    if not text or "No data" in text:
        return result

    reader = csv.DictReader(io.StringIO(text))
    for row in reader:
        result["current_price"] = _parse_float(row.get("Close"))
        result["name"] = row.get("Name", "").strip() or None
        break
    return result


async def _fetch_yahoo_finance_meta(
    client: httpx.AsyncClient,
    ticker: str,
) -> dict:
    """Try to get market cap and PBR from Yahoo Finance JSON API.

    Returns dict with optional keys: market_cap, pbr, current_price, name.
    """
    yahoo_ticker = f"{ticker}.T"
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{yahoo_ticker}"
        f"?range=1d&interval=1d"
    )

    result: dict = {}
    try:
        resp = await client.get(
            url,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.debug("Yahoo Finance request failed for %s: %s", ticker, exc)
        return result

    try:
        chart_result = data["chart"]["result"][0]
        meta = chart_result.get("meta", {})
        result["current_price"] = meta.get("regularMarketPrice")
        # marketCap is not always in chart endpoint; try anyway
        if "marketCap" in meta:
            result["market_cap"] = meta["marketCap"]
        # Short name / long name
        if meta.get("shortName"):
            result["name"] = meta["shortName"]
        elif meta.get("longName"):
            result["name"] = meta["longName"]
    except (KeyError, IndexError, TypeError):
        pass

    return result


async def _fetch_yahoo_quote_summary(
    client: httpx.AsyncClient,
    ticker: str,
) -> dict:
    """Fallback: try Yahoo Finance quoteSummary for market cap / PBR."""
    yahoo_ticker = f"{ticker}.T"
    url = (
        f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{yahoo_ticker}"
        f"?modules=defaultKeyStatistics,price"
    )

    result: dict = {}
    try:
        resp = await client.get(
            url,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.debug("Yahoo quoteSummary failed for %s: %s", ticker, exc)
        return result

    try:
        summary = data["quoteSummary"]["result"][0]

        price_info = summary.get("price", {})
        market_cap_raw = price_info.get("marketCap", {}).get("raw")
        if market_cap_raw:
            result["market_cap"] = market_cap_raw

        name = price_info.get("shortName") or price_info.get("longName")
        if name:
            result["name"] = name

        key_stats = summary.get("defaultKeyStatistics", {})
        pbr_raw = key_stats.get("priceToBook", {}).get("raw")
        if pbr_raw is not None:
            result["pbr"] = round(float(pbr_raw), 2)
    except (KeyError, IndexError, TypeError):
        pass

    return result


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@router.get("/{sec_code}")
async def get_stock_data(sec_code: str) -> dict:
    """Return stock price history, market cap, and PBR for a securities code.

    Accepts EDINET-style 5-digit codes (e.g. ``39320``) or plain 4-digit
    TSE codes (e.g. ``3932``).
    """
    # Check cache first
    cached = _cache_get(sec_code)
    if cached is not None:
        return cached

    ticker = _normalise_sec_code(sec_code)

    async with httpx.AsyncClient(timeout=10.0) as client:
        # Fetch stooq data (history + current quote) in parallel
        # Also try Yahoo Finance for market cap / PBR
        history_task = asyncio.create_task(_fetch_stooq_history(client, ticker))
        quote_task = asyncio.create_task(_fetch_stooq_quote(client, ticker))
        yahoo_meta_task = asyncio.create_task(_fetch_yahoo_finance_meta(client, ticker))
        yahoo_summary_task = asyncio.create_task(_fetch_yahoo_quote_summary(client, ticker))

        history, quote, yahoo_meta, yahoo_summary = await asyncio.gather(
            history_task, quote_task, yahoo_meta_task, yahoo_summary_task,
        )

    # Merge results – prefer Yahoo for market cap / PBR, stooq for prices
    weekly_prices = history

    # Current price: prefer stooq quote, fall back to Yahoo
    current_price = quote.get("current_price")
    if current_price is None:
        current_price = yahoo_meta.get("current_price")

    # Name: prefer Yahoo (usually Japanese), fall back to stooq
    name = (
        yahoo_summary.get("name")
        or yahoo_meta.get("name")
        or quote.get("name")
    )

    # Market cap: from Yahoo
    market_cap = yahoo_summary.get("market_cap") or yahoo_meta.get("market_cap")

    # PBR: from Yahoo quoteSummary
    pbr = yahoo_summary.get("pbr")

    # If we have absolutely nothing, return 404
    if not weekly_prices and current_price is None:
        raise HTTPException(
            status_code=404,
            detail=f"No stock data found for sec_code={sec_code} (ticker={ticker})",
        )

    result: dict = {
        "sec_code": ticker,
        "ticker": f"{ticker}.T",
        "name": name,
        "current_price": current_price,
        "market_cap": market_cap,
        "market_cap_display": _format_market_cap(market_cap),
        "pbr": pbr,
        "weekly_prices": weekly_prices,
    }

    _cache_set(sec_code, result)
    return result
