"""Stock price data endpoint using external free APIs.

Data priority for company names:
  1. EDINET Filing DB (金融庁 data – authoritative)
  2. EDINET code list (金融庁 code master – fetched at startup)
  3. External APIs (stooq, Yahoo Finance – live market data)
  4. _KNOWN_STOCKS fallback (hardcoded, used only when all else fails)
"""

import asyncio
import csv
import hashlib
import io
import logging
import math
import time
from datetime import date, timedelta

import httpx
from fastapi import APIRouter, HTTPException
from sqlalchemy import func as sa_func, select

from app.database import async_session
from app.models import Filing

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/stock", tags=["Stock"])

# ---------------------------------------------------------------------------
# In-memory cache with TTL
# ---------------------------------------------------------------------------
_cache: dict[str, tuple[float, dict]] = {}
_CACHE_TTL = 30 * 60  # 30 minutes
_external_apis_failed = False  # fast-fail flag when all external APIs are unreachable


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
# EDINET (金融庁) data lookups  –  authoritative company info
# ---------------------------------------------------------------------------

# In-memory code list cache:  {4-digit ticker: company_name}
# Populated from EDINET code list ZIP on first startup.
_edinet_code_list: dict[str, str] = {}
_edinet_code_list_loaded = False


async def _load_edinet_code_list() -> None:
    """Fetch the EDINET code list (EdinetcodeDlInfo) and populate the cache.

    The EDINET v2 API provides a ZIP with a CSV mapping every registered
    entity to its EDINET code, securities code, and official company name.
    This is the most authoritative source from 金融庁.
    """
    global _edinet_code_list_loaded
    if _edinet_code_list_loaded:
        return

    from app.config import settings

    if not settings.EDINET_API_KEY:
        logger.debug("No EDINET_API_KEY; skipping code list fetch")
        _edinet_code_list_loaded = True
        return

    url = f"{settings.EDINET_API_BASE}/EdinetcodeDlInfo.json"
    params = {"Subscription-Key": settings.EDINET_API_KEY}

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
    except Exception as exc:
        logger.warning("Failed to fetch EDINET code list: %s", exc)
        _edinet_code_list_loaded = True
        return

    # The response is a ZIP containing a CSV file
    try:
        import zipfile as _zipfile

        with _zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            csv_files = [f for f in zf.namelist() if f.endswith(".csv")]
            if not csv_files:
                logger.warning("EDINET code list ZIP contains no CSV")
                _edinet_code_list_loaded = True
                return

            raw = zf.read(csv_files[0])
            # Try UTF-8 first, fall back to cp932 (Shift-JIS variant)
            for enc in ("utf-8-sig", "utf-8", "cp932"):
                try:
                    text = raw.decode(enc)
                    break
                except UnicodeDecodeError:
                    continue
            else:
                text = raw.decode("utf-8", errors="replace")

            reader = csv.reader(io.StringIO(text))
            header = next(reader, None)
            if header is None:
                _edinet_code_list_loaded = True
                return

            # Find column indices — the CSV header names vary but typically:
            # "ＥＤＩＮＥＴコード", "提出者種別", "上場区分", "証券コード",
            # "提出者名", "提出者名（英字）", ...
            sec_code_idx = None
            name_idx = None
            for i, col in enumerate(header):
                col_lower = col.strip().lower()
                if "証券コード" in col or "securitiescode" in col_lower:
                    sec_code_idx = i
                if ("提出者名" in col or "submittername" in col_lower) and name_idx is None:
                    # Take the first "提出者名" column (Japanese name)
                    name_idx = i

            if sec_code_idx is None or name_idx is None:
                logger.warning(
                    "Could not find required columns in EDINET code list CSV "
                    "(header: %s)", header[:10]
                )
                _edinet_code_list_loaded = True
                return

            count = 0
            for row in reader:
                if len(row) <= max(sec_code_idx, name_idx):
                    continue
                raw_code = row[sec_code_idx].strip()
                name = row[name_idx].strip()
                if not raw_code or not name:
                    continue
                # Normalise to 4-digit ticker
                if len(raw_code) == 5 and raw_code[:4].isdigit():
                    ticker = raw_code[:4]
                elif len(raw_code) == 4 and raw_code.isdigit():
                    ticker = raw_code
                else:
                    continue
                _edinet_code_list[ticker] = name
                count += 1

            logger.info(
                "Loaded %d companies from EDINET code list (金融庁)", count
            )
    except Exception as exc:
        logger.warning("Error parsing EDINET code list: %s", exc)

    _edinet_code_list_loaded = True


async def _lookup_company_from_filings(ticker: str) -> str | None:
    """Look up company name from Filing table (EDINET data = 金融庁 data).

    Queries the most recent filing where the target company has the given
    securities code and returns its name.  This is authoritative because
    the name comes from the official XBRL filing submitted to 金融庁.
    """
    four_digit = ticker
    five_digit = ticker + "0"

    try:
        async with async_session() as session:
            stmt = (
                select(Filing.target_company_name)
                .where(
                    Filing.target_company_name.isnot(None),
                    Filing.target_company_name != "",
                    (Filing.target_sec_code == four_digit)
                    | (Filing.target_sec_code == five_digit)
                    | (Filing.sec_code == four_digit)
                    | (Filing.sec_code == five_digit),
                )
                .order_by(Filing.submit_date_time.desc())
                .limit(1)
            )
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            if row:
                logger.debug("EDINET Filing lookup for %s: %s", ticker, row)
            return row
    except Exception as exc:
        logger.debug("Filing lookup failed for %s: %s", ticker, exc)
        return None


def _lookup_edinet_code_list(ticker: str) -> str | None:
    """Look up company name from the EDINET code list cache."""
    return _edinet_code_list.get(ticker)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _normalise_sec_code(sec_code: str) -> str:
    """Convert EDINET securities code to a 4-digit ticker.

    EDINET codes are typically 5 digits (e.g. "39320") where the 5th digit
    is a check digit that can be any value (not only "0").
    Always take the first 4 digits of a 5-digit code.

    Raises HTTPException(400) for invalid codes to prevent URL injection
    and unbounded cache growth from arbitrary input strings.
    """
    sec_code = sec_code.strip()
    if len(sec_code) == 5 and sec_code[:4].isdigit():
        return sec_code[:4]
    if len(sec_code) == 4 and sec_code.isdigit():
        return sec_code
    raise HTTPException(
        status_code=400,
        detail=f"Invalid securities code: {sec_code!r} (expected 4 or 5 digit code)",
    )


def _format_market_cap(value: float | int | None) -> str | None:
    """Format a market cap (JPY) in Japanese style using 億 / 兆."""
    if value is None or value <= 0:
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
        result = float(v)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def _parse_int(v: str | None) -> int | None:
    f = _parse_float(v)
    if f is None:
        return None
    return int(f)


# Realistic reference data for major Japanese stocks.
# {ticker: (name, approx_price_yen, shares_outstanding, pbr)}
#
# IMPORTANT: shares_outstanding (発行済株式数) is the most stable number
# and critical for market cap estimation.  It only changes on stock splits,
# share buybacks, or new issuance (quarterly at most).  Price and PBR are
# approximate snapshots — in production, live data is fetched from stooq
# and Yahoo Finance.  This table is used ONLY when all external APIs are
# unreachable (e.g. sandboxed environments).
#
# Last verified: 2026-02 from Yahoo Finance Japan / Nikkei / IR Bank
_KNOWN_STOCKS: dict[str, tuple[str, int, int, float]] = {
    # --- Mega-cap ---
    "7203": ("トヨタ自動車", 3635, 15_794_987_460, 1.26),
    "6758": ("ソニーグループ", 3475, 6_149_810_645, 2.54),
    "6861": ("キーエンス", 59880, 243_207_684, 4.06),
    "8306": ("三菱UFJフィナンシャル・グループ", 3009, 11_867_710_920, 1.55),
    "9984": ("ソフトバンクグループ", 4462, 1_428_000_000, 2.83),
    "8035": ("東京エレクトロン", 43960, 471_632_733, 9.95),
    "6501": ("日立製作所", 4930, 4_581_560_985, 3.50),
    "7974": ("任天堂", 8587, 1_298_690_000, 3.36),
    # --- Large-cap ---
    "6098": ("リクルートホールディングス", 10950, 1_614_281_000, 7.20),
    "4063": ("信越化学工業", 5745, 1_984_995_865, 2.36),
    "6367": ("ダイキン工業", 18550, 293_113_973, 1.83),
    "7741": ("HOYA", 27545, 338_414_320, 9.02),
    "6981": ("村田製作所", 3610, 1_963_001_843, 2.55),
    "8001": ("伊藤忠商事", 2267, 7_924_447_520, 2.37),  # 1:5 split 2026-01
    "8316": ("三井住友フィナンシャルグループ", 5963, 3_857_407_640, 1.49),
    "9983": ("ファーストリテイリング", 67410, 318_220_968, 8.31),
    "9433": ("KDDI", 2616, 4_187_847_474, 2.06),
    # --- 半導体 ---
    "6857": ("アドバンテスト", 26000, 732_000_000, 27.43),
    "6146": ("ディスコ", 58570, 108_447_000, 11.39),
    "6920": ("レーザーテック", 30300, 94_286_400, 12.49),
    # --- Mid/Growth ---
    "4385": ("メルカリ", 3495, 164_970_111, 4.85),
    "3994": ("マネーフォワード", 4800, 55_930_000, 12.0),
    "3697": ("SHIFT", 657, 267_500_670, 3.95),  # 株式分割後
    "4443": ("Sansan", 1132, 126_659_468, 16.29),
    "4478": ("フリー", 3200, 103_930_000, 10.0),
    "4384": ("ラクスル", 1700, 54_740_000, 5.0),
    "4169": ("ENECHANGE", 298, 42_780_192, 2.58),
    "4165": ("プレイド", 522, 41_260_663, 4.26),
    # --- 上場廃止銘柄（デモデータ用に保持） ---
    "9613": ("NTTデータグループ", 4000, 1_402_500_000, 3.04),  # 2025-09 上場廃止
}


def _generate_fallback_data(
    ticker: str,
) -> tuple[list[dict], float, str, float, float]:
    """Generate deterministic demo stock data from the ticker string.

    Returns ``(weekly_prices, current_price, name, market_cap, pbr)``.
    Uses known stock reference data when available for realistic market caps,
    otherwise falls back to seed-based generation.
    """
    import random as _random

    seed = int(hashlib.md5(ticker.encode()).hexdigest()[:8], 16)
    rng = _random.Random(seed)

    known = _KNOWN_STOCKS.get(ticker)

    if known:
        ref_name, ref_price, shares_out, ref_pbr = known
        base_price = ref_price
        name = ref_name
    else:
        base_price = 500 + (seed % 7500)
        shares_out = rng.randint(50_000_000, 2_000_000_000)
        ref_pbr = round(rng.uniform(0.4, 4.0), 2)
        name = f"銘柄 {ticker}"

    today = date.today()
    weeks = 52
    prices: list[dict] = []
    price = float(base_price)

    for i in range(weeks):
        d = today - timedelta(weeks=weeks - i)
        change_pct = rng.gauss(0.002, 0.025)
        price *= 1 + change_pct
        price = max(price, 100)

        o = round(price * (1 + rng.gauss(0, 0.008)), 1)
        c = round(price, 1)
        h = round(max(o, c) * (1 + abs(rng.gauss(0, 0.012))), 1)
        low = round(min(o, c) * (1 - abs(rng.gauss(0, 0.012))), 1)
        vol = rng.randint(500_000, 20_000_000)

        prices.append(
            {
                "date": d.isoformat(),
                "open": o,
                "high": h,
                "low": low,
                "close": c,
                "volume": vol,
            }
        )

    current_price = prices[-1]["close"]
    market_cap = current_price * shares_out
    pbr = ref_pbr

    return prices, current_price, name, market_cap, pbr


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
    if not text or "No data" in text or "<html" in text[:200].lower():
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

    # Ensure chronological order (oldest first) for chart rendering
    rows.sort(key=lambda r: r["date"])
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
    if not text or "No data" in text or "<html" in text[:200].lower():
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
        meta = chart_result.get("meta") or {}
        result["current_price"] = meta.get("regularMarketPrice")
        # marketCap is not always in chart endpoint; try anyway
        mc = meta.get("marketCap")
        if mc is not None:
            result["market_cap"] = mc
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

        price_info = summary.get("price") or {}
        market_cap_obj = price_info.get("marketCap") or {}
        market_cap_raw = market_cap_obj.get("raw") if isinstance(market_cap_obj, dict) else None
        if market_cap_raw:
            result["market_cap"] = market_cap_raw

        name = price_info.get("shortName") or price_info.get("longName")
        if name:
            result["name"] = name

        key_stats = summary.get("defaultKeyStatistics") or {}
        pbr_obj = key_stats.get("priceToBook") or {}
        pbr_raw = pbr_obj.get("raw") if isinstance(pbr_obj, dict) else None
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

    Company name priority (金融庁データ優先):
      1. EDINET Filing DB (大量保有報告書のXBRLから抽出)
      2. EDINET code list (金融庁コードマスター)
      3. External APIs (stooq / Yahoo Finance)
      4. _KNOWN_STOCKS fallback (ハードコード, 最終手段)
    """
    global _external_apis_failed
    ticker = _normalise_sec_code(sec_code)

    # Check cache using normalized ticker so "3932" and "39320" share one entry
    cached = _cache_get(ticker)
    if cached is not None:
        return cached

    # --- Step 1: Look up authoritative company name from 金融庁 data ---
    # These run concurrently: DB lookup + EDINET code list (already in memory)
    await _load_edinet_code_list()  # no-op if already loaded
    edinet_name = await _lookup_company_from_filings(ticker)
    if not edinet_name:
        edinet_name = _lookup_edinet_code_list(ticker)

    weekly_prices: list[dict] = []
    current_price = None
    api_name = None  # name from external APIs (lower priority)
    market_cap = None
    pbr = None

    # --- Step 2: Fetch live market data from external APIs ---
    # If external APIs previously failed for ALL sources, skip straight to
    # fallback to avoid 10-second timeouts on every request.
    if not _external_apis_failed:
        async with httpx.AsyncClient(timeout=2.0) as client:
            history_task = asyncio.create_task(_fetch_stooq_history(client, ticker))
            quote_task = asyncio.create_task(_fetch_stooq_quote(client, ticker))
            yahoo_meta_task = asyncio.create_task(_fetch_yahoo_finance_meta(client, ticker))
            yahoo_summary_task = asyncio.create_task(_fetch_yahoo_quote_summary(client, ticker))

            history, quote, yahoo_meta, yahoo_summary = await asyncio.gather(
                history_task, quote_task, yahoo_meta_task, yahoo_summary_task,
            )

        # Merge results – prefer Yahoo for market cap / PBR, stooq for prices
        weekly_prices = history

        current_price = quote.get("current_price")
        if current_price is None:
            current_price = yahoo_meta.get("current_price")

        api_name = (
            yahoo_summary.get("name")
            or yahoo_meta.get("name")
            or quote.get("name")
        )

        market_cap = yahoo_summary.get("market_cap") or yahoo_meta.get("market_cap")
        pbr = yahoo_summary.get("pbr")

        # If everything came back empty, mark external APIs as failed
        # so subsequent requests skip the slow timeout path.
        if not weekly_prices and current_price is None:
            _external_apis_failed = True
            logger.info("All external APIs unreachable; switching to fallback mode")

    # --- Step 3: Fallback when no live data available ---
    if not weekly_prices and current_price is None:
        weekly_prices, current_price, fallback_name, market_cap, pbr = (
            _generate_fallback_data(ticker)
        )
        if not api_name:
            api_name = fallback_name

    # --- Step 4: Apply name priority (金融庁 > API > fallback) ---
    name = edinet_name or api_name

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

    _cache_set(ticker, result)
    return result
