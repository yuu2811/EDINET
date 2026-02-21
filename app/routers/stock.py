"""Stock price data endpoint using external free APIs.

Data priority for company names:
  1. EDINET Filing DB (金融庁 data – authoritative)
  2. EDINET code list (金融庁 code master – fetched at startup)
  3. External APIs (Google Finance, stooq, Yahoo Finance, Kabutan – live market data)
  4. _KNOWN_STOCKS fallback (hardcoded, used only when all else fails)
"""

import asyncio
import csv
import hashlib
import io
import logging
import math
import re
import time
from datetime import date, timedelta

import httpx
from fastapi import APIRouter
from sqlalchemy import func as sa_func, select

from app.database import async_session
from app.deps import validate_sec_code
from app.models import CompanyInfo, Filing

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/stock", tags=["Stock"])

# ---------------------------------------------------------------------------
# In-memory cache with TTL
# ---------------------------------------------------------------------------
_cache: dict[str, tuple[float, dict]] = {}
_CACHE_TTL = 30 * 60  # 30 minutes
_external_apis_failed_at: float = 0.0  # monotonic timestamp; 0 = not failed
_EXTERNAL_RETRY_INTERVAL = 5 * 60  # retry external APIs after 5 minutes


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

    CSV format per API v2 spec (ESE140206):
      - Encoding: cp932 (Windows-31J)
      - Row 1: metadata line (download date, version) — MUST be skipped
      - Row 2: header row (13 columns)
      - Row 3+: data rows
      - Column 7: 提出者名 (submitter name, Japanese)
      - Column 12: 証券コード (securities code, 5-digit with check digit)
    """
    global _edinet_code_list_loaded
    if _edinet_code_list_loaded:
        return

    from app.config import settings

    if not settings.EDINET_API_KEY:
        logger.debug("No EDINET_API_KEY; skipping code list fetch")
        _edinet_code_list_loaded = True
        return

    # EDINET API v2 spec: GET /api/v2/EdinetcodeDlInfo
    # Response: ZIP containing EdinetcodeDlInfo.csv
    url = f"{settings.EDINET_API_BASE}/EdinetcodeDlInfo"
    params = {"Subscription-Key": settings.EDINET_API_KEY}

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
    except Exception as exc:
        logger.warning("Failed to fetch EDINET code list: %s", exc)
        _edinet_code_list_loaded = True
        return

    # The response is a ZIP containing EdinetcodeDlInfo.csv
    try:
        import zipfile as _zipfile

        with _zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            csv_files = [f for f in zf.namelist() if f.endswith(".csv")]
            if not csv_files:
                logger.warning("EDINET code list ZIP contains no CSV")
                _edinet_code_list_loaded = True
                return

            raw = zf.read(csv_files[0])
            # Per API v2 spec: encoding is cp932 (Windows-31J).
            # Try cp932 first, then UTF-8 variants as fallback.
            for enc in ("cp932", "utf-8-sig", "utf-8"):
                try:
                    text = raw.decode(enc)
                    break
                except UnicodeDecodeError:
                    continue
            else:
                text = raw.decode("utf-8", errors="replace")

            reader = csv.reader(io.StringIO(text))

            # Per API v2 spec: Row 1 is metadata (download date etc.) — skip it.
            _metadata_row = next(reader, None)

            # Row 2 is the actual header row with 13 columns.
            header = next(reader, None)
            if header is None:
                _edinet_code_list_loaded = True
                return

            # Find column indices by matching header names.
            # Per spec the 13 columns are (0-indexed):
            #   0: ＥＤＩＮＥＴコード  1: 提出者種別  2: 上場区分
            #   3: 連結の有無  4: 資本金  5: 決算日  6: 提出者名
            #   7: 提出者名（英字）  8: 提出者名（ヨミ）  9: 所在地
            #  10: 提出者業種  11: 証券コード  12: 提出者法人番号
            # Note: header uses full-width characters (ＥＤＩＮＥＴコード).
            sec_code_idx = None
            name_idx = None
            for i, col in enumerate(header):
                col_stripped = col.strip()
                # Match securities code column (full-width or half-width)
                if col_stripped in ("証券コード", "証券ｺｰﾄﾞ") or "証券コード" in col_stripped:
                    sec_code_idx = i
                # Match submitter name column — take the FIRST "提出者名"
                # (column 6, Japanese name), not "提出者名（英字）" (column 7)
                if col_stripped == "提出者名":
                    name_idx = i

            # Fallback: try positional mapping per spec if header matching fails
            if sec_code_idx is None and len(header) >= 12:
                sec_code_idx = 11  # column 12 (0-indexed: 11)
            if name_idx is None and len(header) >= 7:
                name_idx = 6  # column 7 (0-indexed: 6)

            if sec_code_idx is None or name_idx is None:
                logger.warning(
                    "Could not find required columns in EDINET code list CSV "
                    "(header: %s)", header[:13]
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
                # Normalise to 4-digit ticker (strip check digit)
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


async def _lookup_company_info(ticker: str) -> dict | None:
    """Look up CompanyInfo from DB (populated from 有報/四半期報告書).

    Returns dict with shares_outstanding, net_assets, company_name, or None.
    """
    try:
        async with async_session() as session:
            stmt = select(CompanyInfo).where(CompanyInfo.sec_code == ticker)
            result = await session.execute(stmt)
            info = result.scalar_one_or_none()
            if info:
                return info.to_dict()
    except Exception as exc:
        logger.debug("CompanyInfo lookup failed for %s: %s", ticker, exc)
    return None


def _lookup_edinet_code_list(ticker: str) -> str | None:
    """Look up company name from the EDINET code list cache."""
    return _edinet_code_list.get(ticker)


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
    """Fetch detailed stock data from Yahoo Finance quoteSummary.

    Extracts market cap, PBR, shares outstanding, 52-week range,
    change percent, dividend yield, and other financial metrics.
    """
    yahoo_ticker = f"{ticker}.T"
    url = (
        f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{yahoo_ticker}"
        f"?modules=defaultKeyStatistics,price,summaryDetail"
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

    def _raw(obj: dict | None) -> float | int | None:
        """Extract 'raw' value from Yahoo Finance nested dict."""
        if isinstance(obj, dict):
            return obj.get("raw")
        return None

    try:
        summary = data["quoteSummary"]["result"][0]

        # --- price module ---
        price_info = summary.get("price") or {}
        market_cap_raw = _raw(price_info.get("marketCap"))
        if market_cap_raw:
            result["market_cap"] = market_cap_raw

        name = price_info.get("shortName") or price_info.get("longName")
        if name:
            result["name"] = name

        reg_price = _raw(price_info.get("regularMarketPrice"))
        if reg_price:
            result["current_price"] = reg_price

        # Change from previous close
        change = _raw(price_info.get("regularMarketChange"))
        change_pct = _raw(price_info.get("regularMarketChangePercent"))
        if change is not None:
            result["price_change"] = round(float(change), 1)
        if change_pct is not None:
            result["price_change_pct"] = round(float(change_pct) * 100, 2)

        prev_close = _raw(price_info.get("regularMarketPreviousClose"))
        if prev_close:
            result["previous_close"] = prev_close

        # --- defaultKeyStatistics module ---
        key_stats = summary.get("defaultKeyStatistics") or {}

        pbr_raw = _raw(key_stats.get("priceToBook"))
        if pbr_raw is not None:
            result["pbr"] = round(float(pbr_raw), 2)

        shares_raw = _raw(key_stats.get("sharesOutstanding"))
        if shares_raw is not None:
            result["shares_outstanding"] = int(shares_raw)

        # 52-week high/low
        week52_high = _raw(key_stats.get("fiftyTwoWeekHigh"))
        week52_low = _raw(key_stats.get("fiftyTwoWeekLow"))
        if week52_high is not None:
            result["week52_high"] = week52_high
        if week52_low is not None:
            result["week52_low"] = week52_low

        # Enterprise value
        ev = _raw(key_stats.get("enterpriseValue"))
        if ev is not None:
            result["enterprise_value"] = ev

        # --- summaryDetail module ---
        detail = summary.get("summaryDetail") or {}

        dividend_yield = _raw(detail.get("dividendYield"))
        if dividend_yield is not None:
            result["dividend_yield"] = round(float(dividend_yield) * 100, 2)

        trailing_pe = _raw(detail.get("trailingPE"))
        if trailing_pe is not None:
            result["per"] = round(float(trailing_pe), 2)

        # 52-week from summaryDetail (fallback)
        if "week52_high" not in result:
            w52h = _raw(detail.get("fiftyTwoWeekHigh"))
            if w52h is not None:
                result["week52_high"] = w52h
        if "week52_low" not in result:
            w52l = _raw(detail.get("fiftyTwoWeekLow"))
            if w52l is not None:
                result["week52_low"] = w52l

        volume = _raw(detail.get("volume"))
        if volume is not None:
            result["volume"] = int(volume)

    except (KeyError, IndexError, TypeError):
        pass

    return result


# ---------------------------------------------------------------------------
# Google Finance scraper (free, no API key)
# ---------------------------------------------------------------------------

_GOOGLE_FINANCE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,image/webp,*/*;q=0.8"
    ),
}


async def _fetch_google_finance(
    client: httpx.AsyncClient,
    ticker: str,
) -> dict:
    """Scrape current stock price from Google Finance page.

    Google Finance has no public REST API, but stock data is embedded in
    the HTML page.  This scraper uses multiple regex strategies to extract
    the current price — from most to least reliable.

    URL format: https://www.google.com/finance/quote/{TICKER}:TYO
    """
    url = f"https://www.google.com/finance/quote/{ticker}:TYO"
    result: dict = {}

    try:
        resp = await client.get(
            url,
            headers=_GOOGLE_FINANCE_HEADERS,
            follow_redirects=True,
        )
        resp.raise_for_status()
    except Exception as exc:
        logger.debug("Google Finance request failed for %s: %s", ticker, exc)
        return result

    html = resp.text

    # Strategy 1: data-last-price attribute (used in some Google Finance versions)
    m = re.search(r'data-last-price="([\d,.]+)"', html)
    if m:
        try:
            result["current_price"] = float(m.group(1).replace(",", ""))
        except ValueError:
            pass

    # Strategy 2: data-currency-code confirms JPY pricing
    m = re.search(r'data-currency-code="(\w+)"', html)
    if m:
        result["currency"] = m.group(1)

    # Strategy 3: Previous close from data attribute
    m = re.search(r'data-previous-close="([\d,.]+)"', html)
    if m:
        try:
            result["previous_close"] = float(m.group(1).replace(",", ""))
        except ValueError:
            pass

    # Strategy 4: Look for price in JSON-LD structured data
    if "current_price" not in result:
        # Google sometimes embeds structured data with price info
        for pattern in [
            r'"price"\s*:\s*"?([\d,]+(?:\.\d+)?)"?',
            r'"currentPrice"\s*:\s*"?([\d,]+(?:\.\d+)?)"?',
        ]:
            m = re.search(pattern, html)
            if m:
                try:
                    price = float(m.group(1).replace(",", ""))
                    if 1 < price < 10_000_000:  # sanity check for JPY
                        result["current_price"] = price
                        break
                except ValueError:
                    continue

    # Strategy 5: Extract the large displayed price near the ticker
    # Google Finance shows the price prominently, typically as the first
    # large number with ¥ or in a specific div after the ticker heading.
    if "current_price" not in result:
        # Pattern: ¥X,XXX or ¥X,XXX.XX  (with or without comma)
        yen_prices = re.findall(r'[¥￥]([\d,]+(?:\.\d{1,2})?)', html)
        for p in yen_prices:
            try:
                val = float(p.replace(",", ""))
                if 1 < val < 10_000_000:
                    result["current_price"] = val
                    break
            except ValueError:
                continue

    if result.get("current_price"):
        logger.debug(
            "Google Finance price for %s: %s", ticker, result["current_price"]
        )

    return result


# ---------------------------------------------------------------------------
# Kabutan (株探) scraper  –  free Japanese stock data
# ---------------------------------------------------------------------------

async def _fetch_kabutan_quote(
    client: httpx.AsyncClient,
    ticker: str,
) -> dict:
    """Scrape current stock price and company name from Kabutan (株探).

    Kabutan (https://kabutan.jp) is one of Japan's most popular free
    stock information sites.  The stock page has a stable HTML structure
    making it more reliable than Google Finance for Japanese stocks.

    Returns dict with optional keys: current_price, name, market_cap.
    """
    url = f"https://kabutan.jp/stock/?code={ticker}"
    result: dict = {}

    try:
        resp = await client.get(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "ja,en;q=0.9",
            },
            follow_redirects=True,
        )
        resp.raise_for_status()
    except Exception as exc:
        logger.debug("Kabutan request failed for %s: %s", ticker, exc)
        return result

    html = resp.text

    # Company name: <h2>...<h3>社名</h3></h2> or <title>【XXXX】社名 ... </title>
    m = re.search(r'<title>[^<]*?】\s*(.+?)\s*[\|｜<]', html)
    if m:
        name = m.group(1).strip()
        # Remove trailing stock info like "の株価・株式情報"
        name = re.sub(r'の株価.*$', '', name).strip()
        if name:
            result["name"] = name

    # Current price: Kabutan shows the price in a <span class="kabuprice"> or
    # in the stock_price table.  Look for multiple patterns.

    # Pattern 1: <td class="stock_price">X,XXX</td> or similar
    price_patterns = [
        # 株価 (stock price) in the main price display area
        r'class="[^"]*stock_price[^"]*"[^>]*>\s*([,\d]+(?:\.\d+)?)',
        # kabuprice class
        r'class="[^"]*kabuprice[^"]*"[^>]*>\s*([,\d]+(?:\.\d+)?)',
        # The "現在値" (current price) cell
        r'現在値[^<]*</[^>]+>\s*<[^>]+>\s*([,\d]+(?:\.\d+)?)',
        # Price in a dd or span near 株価
        r'株価[^<]*<[^>]+>\s*([,\d]+(?:\.\d+)?)',
    ]

    for pattern in price_patterns:
        m = re.search(pattern, html)
        if m:
            try:
                price_str = m.group(1).replace(",", "")
                price = float(price_str)
                if 1 < price < 10_000_000:
                    result["current_price"] = price
                    break
            except ValueError:
                continue

    # Fallback: just find the first comma-separated number in a prominent position
    if "current_price" not in result:
        # Look near the beginning of the page body for a prominent number
        body_start = html.find('<body')
        if body_start > 0:
            body_chunk = html[body_start:body_start + 5000]
            # Find numbers that look like stock prices (3-7 digits, with commas)
            nums = re.findall(r'>([,\d]{3,9}(?:\.\d{1,2})?)<', body_chunk)
            for n in nums:
                try:
                    val = float(n.replace(",", ""))
                    if 50 < val < 10_000_000:
                        result["current_price"] = val
                        break
                except ValueError:
                    continue

    # Market cap: sometimes shown as "時価総額" followed by a number in 億 or 百万
    m = re.search(r'時価総額[^<]*?([,\d]+(?:\.\d+)?)\s*(?:百万|億)', html)
    if m:
        try:
            mc_str = m.group(1).replace(",", "")
            mc_val = float(mc_str)
            # Check if unit is 百万 (millions) or 億 (100 millions)
            if "億" in html[m.start():m.end() + 5]:
                result["market_cap"] = mc_val * 100_000_000
            else:
                result["market_cap"] = mc_val * 1_000_000
        except ValueError:
            pass

    if result.get("current_price"):
        logger.debug(
            "Kabutan price for %s: %s", ticker, result["current_price"]
        )

    return result


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@router.get("/{sec_code}")
async def get_stock_data(sec_code: str) -> dict:
    """Return stock price history, market cap, and PBR for a securities code.

    Accepts EDINET-style 5-digit codes (e.g. ``39320``) or plain 4-digit
    TSE codes (e.g. ``3932``).

    Data sources (all free, no API keys required for stock prices):
      - stooq.com: price history (CSV) + current quote
      - Google Finance: current price (HTML scraping)
      - Yahoo Finance: chart meta + quoteSummary (JSON)
      - Kabutan (株探): current price + company name (HTML scraping)

    Company name priority (金融庁データ優先):
      1. EDINET Filing DB (大量保有報告書のXBRLから抽出)
      2. EDINET code list (金融庁コードマスター)
      3. External APIs (Google Finance / stooq / Yahoo / Kabutan)
      4. _KNOWN_STOCKS fallback (ハードコード, 最終手段)
    """
    global _external_apis_failed_at
    ticker = validate_sec_code(sec_code)

    # Check cache using normalized ticker so "3932" and "39320" share one entry
    cached = _cache_get(ticker)
    if cached is not None:
        return cached

    # --- Step 1: Look up authoritative data from 金融庁 (EDINET) ---
    await _load_edinet_code_list()  # no-op if already loaded

    # Company name: Filing DB > Code list
    edinet_name = await _lookup_company_from_filings(ticker)
    if not edinet_name:
        edinet_name = _lookup_edinet_code_list(ticker)

    # Company fundamentals: CompanyInfo table (from 有報/四半期報告書)
    company_info = await _lookup_company_info(ticker)
    edinet_shares = company_info.get("shares_outstanding") if company_info else None
    edinet_bps = company_info.get("bps") if company_info else None
    if company_info and not edinet_name:
        edinet_name = company_info.get("company_name")

    weekly_prices: list[dict] = []
    current_price = None
    api_name = None  # name from external APIs (lower priority)
    market_cap = None
    pbr = None
    price_source = "fallback"
    yahoo_summary: dict = {}  # populated by Yahoo quoteSummary if APIs are reachable

    # --- Step 2: Fetch live market data from external APIs ---
    # 6 sources fetched in parallel (all free, no API keys):
    #   - stooq: price history + current quote (CSV API)
    #   - Yahoo Finance: chart meta + quoteSummary (JSON API)
    #   - Google Finance: page scraping (HTML)
    #   - Kabutan (株探): page scraping (HTML, Japanese stock specialist)
    #
    # If external APIs previously failed for ALL sources, skip straight to
    # fallback to avoid 10-second timeouts on every request.
    apis_available = (
        _external_apis_failed_at == 0.0
        or (time.monotonic() - _external_apis_failed_at) > _EXTERNAL_RETRY_INTERVAL
    )
    if apis_available:
        async with httpx.AsyncClient(timeout=4.0) as client:
            history_task = asyncio.create_task(_fetch_stooq_history(client, ticker))
            quote_task = asyncio.create_task(_fetch_stooq_quote(client, ticker))
            yahoo_meta_task = asyncio.create_task(_fetch_yahoo_finance_meta(client, ticker))
            yahoo_summary_task = asyncio.create_task(_fetch_yahoo_quote_summary(client, ticker))
            google_task = asyncio.create_task(_fetch_google_finance(client, ticker))
            kabutan_task = asyncio.create_task(_fetch_kabutan_quote(client, ticker))

            (
                history, quote, yahoo_meta, yahoo_summary,
                google_data, kabutan_data,
            ) = await asyncio.gather(
                history_task, quote_task, yahoo_meta_task, yahoo_summary_task,
                google_task, kabutan_task,
            )

        # Merge results – prefer Yahoo for market cap / PBR, stooq for prices,
        # Google Finance and Kabutan as additional price sources
        weekly_prices = history

        # Current price priority (with source tracking):
        #   1. stooq (most reliable for Japanese stocks)
        #   2. Google Finance (real-time, free)
        #   3. Yahoo Finance quoteSummary
        #   4. Yahoo Finance chart meta
        #   5. Kabutan (株探)
        price_source = "fallback"
        for source_name, source_price in [
            ("stooq", quote.get("current_price")),
            ("google_finance", google_data.get("current_price")),
            ("yahoo_summary", yahoo_summary.get("current_price")),
            ("yahoo_chart", yahoo_meta.get("current_price")),
            ("kabutan", kabutan_data.get("current_price")),
        ]:
            if source_price is not None:
                current_price = source_price
                price_source = source_name
                break

        # Company name from APIs (lower priority than EDINET)
        api_name = (
            yahoo_summary.get("name")
            or yahoo_meta.get("name")
            or kabutan_data.get("name")
            or quote.get("name")
        )

        shares_outstanding = yahoo_summary.get("shares_outstanding")
        pbr = yahoo_summary.get("pbr")

        # Market cap: EDINET shares × live price is most accurate
        best_shares = edinet_shares or shares_outstanding
        if best_shares and current_price:
            market_cap = best_shares * current_price
        else:
            market_cap = (
                yahoo_summary.get("market_cap")
                or yahoo_meta.get("market_cap")
                or kabutan_data.get("market_cap")
            )

        # PBR: EDINET BPS is more accurate when available
        if edinet_bps and current_price and edinet_bps > 0:
            pbr = round(current_price / edinet_bps, 2)

        # If everything came back empty from ALL sources, mark as failed
        # so subsequent requests skip the slow timeout path.
        if not weekly_prices and current_price is None:
            _external_apis_failed_at = time.monotonic()
            logger.info("All external APIs unreachable; fallback mode for %ds", _EXTERNAL_RETRY_INTERVAL)
        else:
            _external_apis_failed_at = 0.0  # APIs working again

    # --- Step 3: Fallback when no live data available ---
    extra: dict = {}
    if not weekly_prices and current_price is None:
        weekly_prices, current_price, fallback_name, market_cap, pbr = (
            _generate_fallback_data(ticker)
        )
        price_source = "fallback"
        if not api_name:
            api_name = fallback_name
    else:
        # Collect enriched data from Yahoo Finance
        if yahoo_summary:
            for key in (
                "per", "dividend_yield", "volume",
                "week52_high", "week52_low",
                "price_change", "price_change_pct", "previous_close",
            ):
                val = yahoo_summary.get(key)
                if val is not None:
                    extra[key] = val

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
        "price_source": price_source,
        **extra,
    }

    _cache_set(ticker, result)
    return result
