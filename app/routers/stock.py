"""Stock price data endpoint using external free APIs.

Data priority for company names:
  1. EDINET Filing DB (金融庁 data – authoritative)
  2. EDINET code list (金融庁 code master – fetched at startup)
  3. External APIs (Google Finance, stooq, Yahoo Finance, Kabutan – live market data)
"""

import asyncio
import csv
import io
import logging
import math
import re
import time
from datetime import date, timedelta

import httpx
from fastapi import APIRouter
from sqlalchemy import select

from app.database import async_session
from app.deps import validate_sec_code
from app.models import CompanyInfo, Filing

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/stock", tags=["Stock"])

# ---------------------------------------------------------------------------
# In-memory cache with TTL
# ---------------------------------------------------------------------------
_cache: dict[str, tuple[float, dict]] = {}
_CACHE_TTL: int = 0  # populated lazily from settings
_external_apis_failed_at: float = 0.0  # monotonic timestamp; 0 = not failed
_EXTERNAL_RETRY_INTERVAL = 5 * 60  # retry external APIs after 5 minutes


def _get_cache_ttl() -> int:
    global _CACHE_TTL
    if _CACHE_TTL == 0:
        from app.config import settings
        _CACHE_TTL = settings.STOCK_CACHE_TTL
    return _CACHE_TTL


def _cache_get(key: str) -> dict | None:
    """Return cached value if present and not expired."""
    entry = _cache.get(key)
    if entry is None:
        return None
    ts, value = entry
    if time.monotonic() - ts > _get_cache_ttl():
        _cache.pop(key, None)
        return None
    return value


_CACHE_MAX_SIZE = 500


def _cache_set(key: str, value: dict) -> None:
    # Evict expired entries when cache grows too large
    if len(_cache) >= _CACHE_MAX_SIZE:
        now = time.monotonic()
        ttl = _get_cache_ttl()
        expired_keys = [k for k, (ts, _) in _cache.items() if now - ts > ttl]
        for k in expired_keys:
            _cache.pop(k, None)
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
    """Scrape current stock price and market cap from Google Finance page.

    Google Finance has no public REST API, but stock data is embedded in
    the HTML page.  This scraper uses multiple regex strategies to extract
    the current price and market cap — from most to least reliable.

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

    # --- Market cap extraction ---
    # Google Finance shows 時価総額 (Market cap) in the stats section.
    # Patterns: "時価総額" followed by a value with unit (兆/億), or
    # "Market cap" followed by a numeric value with T/B suffix.
    _oku = 100_000_000  # 億
    _cho = 1_000_000_000_000  # 兆
    for mc_pattern in [
        # Japanese: "X.XX兆 JPY" or "X,XXX億 JPY" (allow tags between)
        r'時価総額.*?([\d,]+(?:\.\d+)?)\s*兆',
        r'時価総額.*?([\d,]+(?:\.\d+)?)\s*億',
        # English: "XX.XXT JPY" (T=trillion) or "XX.XXB JPY" (B=billion)
        r'Market cap.*?([\d,.]+)\s*T\b',
        r'Market cap.*?([\d,.]+)\s*B\b',
    ]:
        mc_match = re.search(mc_pattern, html, re.IGNORECASE | re.DOTALL)
        if mc_match:
            try:
                mc_val = float(mc_match.group(1).replace(",", ""))
                if "兆" in mc_pattern or "T\\b" in mc_pattern:
                    result["market_cap"] = mc_val * _cho
                elif "億" in mc_pattern:
                    result["market_cap"] = mc_val * _oku
                else:
                    # B = billion = 10億
                    result["market_cap"] = mc_val * 1_000_000_000
                break
            except ValueError:
                continue

    if result.get("current_price"):
        logger.debug(
            "Google Finance price for %s: %s (market_cap=%s)",
            ticker, result["current_price"], result.get("market_cap"),
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
    # Allow HTML tags between label and value (e.g. </th><td>...)
    mc_patterns = [
        r'時価総額.*?([,\d]+(?:\.\d+)?)\s*億',
        r'時価総額.*?([,\d]+(?:\.\d+)?)\s*百万',
    ]
    for mc_pat in mc_patterns:
        m = re.search(mc_pat, html, re.DOTALL)
        if m:
            try:
                mc_val = float(m.group(1).replace(",", ""))
                if "億" in mc_pat:
                    result["market_cap"] = mc_val * 100_000_000
                else:
                    result["market_cap"] = mc_val * 1_000_000
                break
            except ValueError:
                continue

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

            _gather_results = await asyncio.gather(
                history_task, quote_task, yahoo_meta_task, yahoo_summary_task,
                google_task, kabutan_task,
                return_exceptions=True,
            )
            # Replace any failed tasks with safe empty defaults
            _defaults = ([], {}, {}, {}, {}, {})
            history, quote, yahoo_meta, yahoo_summary, google_data, kabutan_data = (
                r if not isinstance(r, BaseException) else d
                for r, d in zip(_gather_results, _defaults)
            )
            for _i, _r in enumerate(_gather_results):
                if isinstance(_r, BaseException):
                    logger.warning("Stock data source %d failed for %s: %s", _i, ticker, _r)

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
                or google_data.get("market_cap")
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

    # --- Step 3: Collect enriched data from Yahoo Finance ---
    extra: dict = {}
    if yahoo_summary:
        for key in (
            "per", "dividend_yield", "volume",
            "week52_high", "week52_low",
            "price_change", "price_change_pct", "previous_close",
        ):
            val = yahoo_summary.get(key)
            if val is not None:
                extra[key] = val

    # --- Step 4: Apply name priority (金融庁 > API) ---
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
