"""EDINET API v2 client for fetching large shareholding reports.

XBRL前回保有割合 (previous_holding_ratio) の検出ロジック:

EDINET大量保有報告書のXBRLでは、今回と前回の保有割合を**同じcontextRef**
(FilingDateInstant) で記録し、**要素名**で区別する。
contextRefの "Prior"/"Previous" による区別はフォールバックとして残存。

対応する要素名パターン (jplvh_cor namespace):
  1. HoldingRatioOfShareCertificatesEtcPerLastReport  — 実EDINET確認済み
  2. PreviousHoldingRatioOfShareCertificatesEtc        — EdinetUtility確認済み
  3. RatioOfShareCertificatesEtcAtTimeOfPreviousReport  — タクソノミ命名規則

is_previous 判定 (全抽出パスで共通):
  - "PerLastReport" in element_name
  - "Previous" in element_name
  - "Prior" in contextRef
  - "Previous" in contextRef
"""

import io
import logging
import re
import zipfile
from datetime import date

import httpx
from lxml import etree

from app.config import settings

logger = logging.getLogger(__name__)

# XBRL namespaces commonly used in large shareholding reports
XBRL_NS = {
    "xbrli": "http://www.xbrl.org/2003/instance",
    "xlink": "http://www.w3.org/1999/xlink",
}

# Inline XBRL namespace
IX_NS = "http://www.xbrl.org/2013/inlineXBRL"


def _empty_holding_result() -> dict:
    """Return a fresh empty holding data dict."""
    return {
        "holding_ratio": None,
        "previous_holding_ratio": None,
        "holder_name": None,
        "target_company_name": None,
        "target_sec_code": None,
        "shares_held": None,
        "purpose_of_holding": None,
    }


def _is_previous_ratio(local_name: str, context_ref: str) -> bool:
    """Determine whether a ratio element represents the *previous* holding ratio.

    Checks element name and contextRef for "PerLastReport", "Previous", or "Prior".
    """
    return (
        "PerLastReport" in local_name
        or "Previous" in local_name
        or "Prior" in context_ref
        or "Previous" in context_ref
    )


def _normalize_ratio(val: float) -> float:
    """Convert decimal-format ratios (0.0523) to percentage (5.23).

    EDINET stores as percentage but some filings use decimal.
    """
    if 0 < val < 1.0:
        return round(val * 100, 4)
    return val


def _looks_like_pdf(content: bytes) -> bool:
    """Return True when bytes appear to contain a PDF header.

    Some upstream servers prepend whitespace/BOM bytes before "%PDF".
    The PDF signature is allowed to appear within the first 1024 bytes.
    """
    return b"%PDF" in content[:1024]


class EdinetClient:
    """Async client for the EDINET API v2."""

    def __init__(self):
        self.base_url = settings.EDINET_API_BASE
        self.api_key = settings.EDINET_API_KEY
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def _fetch_documents_raw(self, target_date: date) -> list[dict]:
        """Fetch the raw document list for a date from EDINET API.

        Shared implementation used by both filtered and unfiltered list endpoints.
        """
        client = await self._get_client()
        url = f"{self.base_url}/documents.json"
        params = {
            "date": target_date.strftime("%Y-%m-%d"),
            "type": 2,  # Return document list + metadata
            "Subscription-Key": self.api_key,
        }

        try:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPStatusError as e:
            logger.error("EDINET API HTTP error: %s %s", e.response.status_code, e)
            return []
        except Exception as e:
            logger.error("EDINET API request failed: %s", e)
            return []

        metadata = data.get("metadata", {})
        status = metadata.get("status")
        if status != "200":
            logger.warning(
                "EDINET API returned status %s: %s",
                status,
                metadata.get("message"),
            )
            return []

        return data.get("results", [])

    async def fetch_document_list(
        self, target_date: date
    ) -> list[dict]:
        """Fetch the document list for a given date from EDINET API.

        Returns only large shareholding reports (docTypeCode 350/360).
        """
        results = await self._fetch_documents_raw(target_date)
        filings = []
        for doc in results:
            doc_type = doc.get("docTypeCode")
            if doc_type not in settings.LARGE_HOLDING_DOC_TYPES:
                continue

            # API v2 spec: filter out withdrawn documents
            # withdrawalStatus: "0"=none, "1"=withdrawn, "2"=withdrawal of withdrawal
            withdrawal = doc.get("withdrawalStatus", "0")
            if withdrawal == "1":
                logger.debug(
                    "Skipping withdrawn document %s", doc.get("docID")
                )
                continue

            # API v2 spec: filter out non-disclosed documents
            # disclosureStatus: "0"=disclosed, "1"=not disclosed
            disclosure = doc.get("disclosureStatus", "0")
            if disclosure != "0":
                logger.debug(
                    "Skipping non-disclosed document %s (status=%s)",
                    doc.get("docID"), disclosure,
                )
                continue

            filings.append(doc)

        logger.info(
            "Fetched %d large shareholding filings for %s (total docs: %d)",
            len(filings),
            target_date,
            len(results),
        )
        return filings

    async def _download_document(
        self, doc_id: str, doc_type: int, label: str,
    ) -> httpx.Response | None:
        """Download a document from EDINET and validate the response.

        Returns the httpx.Response on success, or None if the request
        failed or the response looks like an error page.
        """
        client = await self._get_client()
        url = f"{self.base_url}/documents/{doc_id}"
        params = {
            "type": doc_type,
            "Subscription-Key": self.api_key,
        }

        try:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            logger.warning(
                "EDINET API returned HTTP %s for %s download of %s",
                e.response.status_code, label, doc_id,
            )
            return None
        except Exception as e:
            logger.error("Failed to download %s for %s: %s", label, doc_id, e)
            return None

        # EDINET API may return HTTP 200 with a JSON/HTML error body
        ct = resp.headers.get("content-type", "")
        if "json" in ct or "text/html" in ct:
            body_preview = resp.content[:500].decode("utf-8", errors="replace")
            logger.warning(
                "EDINET API returned non-document Content-Type '%s' for %s %s: %s",
                ct, label, doc_id, body_preview,
            )
            return None

        return resp

    async def download_xbrl(self, doc_id: str) -> bytes | None:
        """Download the XBRL ZIP for a given document ID."""
        resp = await self._download_document(doc_id, doc_type=1, label="XBRL")
        if resp is None:
            return None

        if len(resp.content) < 100:
            logger.warning(
                "EDINET XBRL response too small for %s (%d bytes)",
                doc_id, len(resp.content),
            )
            return None

        return resp.content

    async def download_pdf(self, doc_id: str) -> bytes | None:
        """Download the PDF for a given document ID (type=2).

        EDINET API v2 spec (Jan 2026):
        - type=2 returns the PDF file directly as binary data.
        - Older behaviour returned a ZIP containing PDFs; we handle both.
        - On error the API may return HTTP 200 with a JSON body instead
          of document data — we detect this via Content-Type.
        """
        resp = await self._download_document(doc_id, doc_type=2, label="PDF")
        if resp is None:
            return None

        # Try ZIP first (older API / some document types return ZIP containing PDF)
        try:
            with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
                pdf_files = [
                    f for f in zf.namelist()
                    if f.lower().endswith(".pdf")
                ]
                if pdf_files:
                    return zf.read(pdf_files[0])
                logger.warning("No PDF files found in ZIP for %s", doc_id)
                return None
        except zipfile.BadZipFile:
            pass

        # Directly returned PDF (current API v2 spec, Jan 2026)
        if _looks_like_pdf(resp.content):
            return resp.content

        logger.warning(
            "EDINET returned neither valid PDF nor ZIP for %s "
            "(%d bytes, content-type=%s)",
            doc_id, len(resp.content),
            resp.headers.get("content-type", ""),
        )
        return None

    def parse_xbrl_for_holding_data(self, zip_content: bytes) -> dict:
        """Parse XBRL ZIP to extract shareholding data.

        Handles both traditional XBRL (.xbrl) and inline XBRL (.htm/.xhtml)
        formats.  EDINET large shareholding reports (docTypeCode 350/360)
        typically use inline XBRL since ~2019.

        Returns a dict with keys:
        - holding_ratio: float | None
        - previous_holding_ratio: float | None
        - holder_name: str | None
        - target_company_name: str | None
        - target_sec_code: str | None
        - shares_held: int | None
        - purpose_of_holding: str | None
        """
        result = _empty_holding_result()

        try:
            with zipfile.ZipFile(io.BytesIO(zip_content)) as zf:
                all_files = zf.namelist()
                logger.debug("XBRL ZIP contains %d files: %s",
                             len(all_files), all_files[:20])

                # Collect candidate files.  Prefer PublicDoc/ but fall back
                # to any .xbrl/.htm/.xhtml in the ZIP (EDINET large holding
                # reports may use a flat structure without PublicDoc/).
                xbrl_pub = [f for f in all_files
                            if f.endswith(".xbrl") and "PublicDoc" in f]
                xbrl_any = [f for f in all_files
                            if f.endswith(".xbrl")
                            and "AuditDoc" not in f
                            and "__MACOSX" not in f]
                xbrl_files = xbrl_pub or xbrl_any

                htm_pub = [f for f in all_files
                           if "PublicDoc" in f
                           and (f.endswith(".htm") or f.endswith(".xhtml"))]
                htm_any = [f for f in all_files
                           if (f.endswith(".htm") or f.endswith(".xhtml"))
                           and "AuditDoc" not in f
                           and "__MACOSX" not in f]
                htm_files = htm_pub or htm_any

                # --- Try 1: traditional XBRL instance (.xbrl) ---
                if xbrl_files:
                    xbrl_content = zf.read(xbrl_files[0])
                    result = self._extract_from_xbrl(xbrl_content)
                    if result["holding_ratio"] is not None:
                        logger.debug("Extracted data from traditional XBRL: %s", xbrl_files[0])
                        return result

                # --- Try 2: inline XBRL (.htm / .xhtml) ---
                if htm_files:
                    logger.debug("Trying inline XBRL from %d .htm files", len(htm_files))
                    for htm_file in htm_files:
                        htm_content = zf.read(htm_file)
                        inline_result = self._extract_from_inline_xbrl(htm_content)
                        if inline_result["holding_ratio"] is not None:
                            logger.debug("Extracted data from inline XBRL: %s", htm_file)
                            return inline_result
                    # Use best result from inline parsing (might have partial data)
                    if htm_files:
                        # Merge results: take first non-None value from any .htm file
                        merged = dict(result)
                        for htm_file in htm_files:
                            htm_content = zf.read(htm_file)
                            partial = self._extract_from_inline_xbrl(htm_content)
                            for key in merged:
                                if merged[key] is None and partial[key] is not None:
                                    merged[key] = partial[key]
                        if any(v is not None for v in merged.values()):
                            logger.debug("Merged partial inline XBRL data from %d files", len(htm_files))
                            return merged

                if not xbrl_files and not htm_files:
                    logger.warning(
                        "No XBRL (.xbrl) or inline XBRL (.htm) files in PublicDoc/. "
                        "ZIP contents: %s", all_files[:20],
                    )

        except zipfile.BadZipFile:
            logger.warning("Invalid ZIP file received from EDINET")
        except Exception as e:
            logger.error("XBRL parsing error: %s", e)

        return result

    def _extract_from_xbrl(self, xbrl_bytes: bytes) -> dict:
        """Extract holding data from XBRL instance XML.

        前回保有割合の検出:
          要素名に PerLastReport / Previous を含むか、
          contextRef に Prior / Previous を含む場合は previous_holding_ratio に格納。
          ratio_patterns を全てスキャンし、holding_ratio と previous_holding_ratio の
          両方が見つかるまでループを継続する。
        """
        result = _empty_holding_result()

        try:
            tree = etree.fromstring(xbrl_bytes)
        except etree.XMLSyntaxError as e:
            logger.warning("XBRL XML parse error: %s", e)
            return result

        # Use local-name() to match elements regardless of namespace prefix.
        # Large shareholding report XBRL uses jpcrp060_cor namespace.

        # --- Holding ratio (保有割合) ---
        # Must match both jpcrp_cor and jplvh_cor taxonomy:
        #   jpcrp_cor: TotalShareholdingRatioOfShareCertificatesEtc
        #   jplvh_cor: HoldingRatioOfShareCertificatesEtc
        #   jplvh_cor: RatioOfShareCertificatesEtcAtTimeOfPreviousReport (前回保有割合)
        # Priority: specific > generic.  Exclude abstract elements
        # and individual shareholder entries (EachLargeShareholder1, etc.)
        ratio_patterns = [
            "HoldingRatioOfShareCertificatesEtc",  # jplvh_cor (大量保有)
            "TotalShareholdingRatioOfShareCertificatesEtc",  # jpcrp_cor
            "TotalShareholdingRatio",
            "RatioOfShareholdingToTotalIssuedShares",
            "RatioOfShareCertificatesEtcAtTimeOfPreviousReport",  # jplvh_cor 前回保有割合
        ]
        for pattern in ratio_patterns:
            elements = tree.xpath(
                f"//*[contains(local-name(), '{pattern}')]"
            )
            if elements:
                for elem in elements:
                    local = elem.xpath("local-name()")
                    # Skip abstract/header elements and individual holder entries
                    if "Abstract" in local or "EachLargeShareholder" in local:
                        continue
                    try:
                        val = _normalize_ratio(float(elem.text.strip()))
                        context_ref = elem.get("contextRef", "")
                        if _is_previous_ratio(local, context_ref):
                            if result["previous_holding_ratio"] is None:
                                result["previous_holding_ratio"] = val
                        else:
                            if result["holding_ratio"] is None:
                                result["holding_ratio"] = val
                    except (ValueError, AttributeError):
                        continue
                if result["holding_ratio"] is not None and result["previous_holding_ratio"] is not None:
                    break

        # Fallback: broader search if specific patterns didn't match
        if result["holding_ratio"] is None:
            elements = tree.xpath(
                "//*[contains(local-name(), 'HoldingRatio')]"
            )
            for elem in elements:
                local = elem.xpath("local-name()")
                if any(skip in local for skip in (
                    "Abstract", "EachLargeShareholder", "JointHolder",
                )):
                    continue
                try:
                    val = _normalize_ratio(float(elem.text.strip()))
                    context_ref = elem.get("contextRef", "")
                    if _is_previous_ratio(local, context_ref):
                        if result["previous_holding_ratio"] is None:
                            result["previous_holding_ratio"] = val
                    else:
                        if result["holding_ratio"] is None:
                            result["holding_ratio"] = val
                except (ValueError, AttributeError):
                    continue

        # --- Holder name (報告義務発生者 / 提出者) ---
        holder_patterns = [
            "NameOfLargeShareholdingReporter",
            "NameOfFiler",
            "ReporterName",
            "LargeShareholderName",
        ]
        for pattern in holder_patterns:
            elements = tree.xpath(
                f"//*[contains(local-name(), '{pattern}')]"
            )
            for elem in elements:
                if elem.text and elem.text.strip():
                    result["holder_name"] = elem.text.strip()
                    break
            if result["holder_name"]:
                break

        # jplvh_cor fallback: exact local-name() = 'Name' within jplvh namespace
        if result["holder_name"] is None:
            elements = tree.xpath("//*[local-name() = 'Name']")
            for elem in elements:
                ns_uri = elem.tag.split("}")[0].lstrip("{") if "}" in elem.tag else ""
                if "jplvh" in ns_uri or "lvh" in ns_uri:
                    if elem.text and elem.text.strip():
                        result["holder_name"] = elem.text.strip()
                        break

        # --- Target company name (発行者 / 対象会社) ---
        target_patterns = [
            "IssuerNameLargeShareholding",
            "IssuerName",
            "NameOfIssuer",
            "TargetCompanyName",
        ]
        for pattern in target_patterns:
            elements = tree.xpath(
                f"//*[contains(local-name(), '{pattern}')]"
            )
            for elem in elements:
                if elem.text and elem.text.strip():
                    result["target_company_name"] = elem.text.strip()
                    break
            if result["target_company_name"]:
                break

        # --- Target securities code ---
        sec_code_patterns = [
            "SecurityCodeOfIssuer",
            "IssuerSecuritiesCode",
            "SecurityCode",
        ]
        for pattern in sec_code_patterns:
            elements = tree.xpath(
                f"//*[contains(local-name(), '{pattern}')]"
            )
            for elem in elements:
                if elem.text and elem.text.strip():
                    result["target_sec_code"] = elem.text.strip()
                    break
            if result["target_sec_code"]:
                break

        # --- Total shares held ---
        shares_patterns = [
            "TotalNumberOfShareCertificatesEtcHeld",
            "TotalNumberOfSharesHeld",
            "NumberOfShareCertificatesEtc",
            "NumberOfStocksEtcHeld",  # jplvh_cor: TotalNumberOfStocksEtcHeld
        ]
        for pattern in shares_patterns:
            elements = tree.xpath(
                f"//*[contains(local-name(), '{pattern}')]"
            )
            for elem in elements:
                try:
                    val = int(float(elem.text.strip()))
                    context_ref = elem.get("contextRef", "")
                    if "Prior" not in context_ref and "Previous" not in context_ref:
                        result["shares_held"] = val
                        break
                except (ValueError, AttributeError):
                    continue
            if result["shares_held"] is not None:
                break

        # --- Purpose of holding (保有目的) ---
        purpose_patterns = [
            "PurposeOfHolding",
            "PurposeOfHoldingOfShareCertificatesEtc",
        ]
        for pattern in purpose_patterns:
            elements = tree.xpath(
                f"//*[contains(local-name(), '{pattern}')]"
            )
            for elem in elements:
                if elem.text and elem.text.strip():
                    result["purpose_of_holding"] = elem.text.strip()
                    break
            if result["purpose_of_holding"]:
                break

        logger.debug("Extracted XBRL data: %s", result)
        return result

    def _extract_from_inline_xbrl(self, htm_bytes: bytes) -> dict:
        """Extract holding data from inline XBRL (iXBRL) .htm files.

        Inline XBRL embeds structured data in XHTML using ix: namespace
        tags.  Numeric values use <ix:nonFraction name="ns:ElementName">
        and text values use <ix:nonNumeric name="ns:ElementName">.

        Strategy:
        1. Try XML parser (preserves namespaces) — works for well-formed XHTML
        2. Fall back to regex extraction — works even when parsers fail
        """
        result = _empty_holding_result()

        # --- Strategy 1: XML parser (namespace-aware) ---
        try:
            tree = etree.fromstring(htm_bytes)
            result = self._extract_inline_via_xml(tree)
            if result["holding_ratio"] is not None:
                logger.debug("Inline XBRL: extracted via XML parser")
                return result
        except etree.XMLSyntaxError:
            logger.debug("Inline XBRL: XML parse failed, trying regex")
        except Exception as e:
            logger.debug("Inline XBRL: XML parse error: %s", e)

        # --- Strategy 2: Regex extraction (robust fallback) ---
        result2 = self._extract_inline_via_regex(htm_bytes)
        # Merge: prefer XML results, fill gaps with regex
        for key in result:
            if result[key] is None and result2[key] is not None:
                result[key] = result2[key]

        if result["holding_ratio"] is not None:
            logger.debug("Inline XBRL: extracted via regex")
        else:
            logger.warning("Inline XBRL: no holding_ratio found by any method")

        return result

    def _extract_inline_via_xml(self, tree) -> dict:
        """Extract inline XBRL data using namespace-aware XML tree.

        ix:nonFraction 要素の name 属性からローカル名を取得し、
        _matches_ratio_pattern() でマッチした要素について is_previous 判定を行う。
        """
        result = _empty_holding_result()

        # Discover the ix namespace URI dynamically from the document
        nsmap = {}
        for elem in tree.iter():
            if hasattr(elem, "nsmap"):
                nsmap.update(elem.nsmap)
                break

        ix_uri = nsmap.get("ix", IX_NS)

        # Find ix:nonFraction and ix:nonNumeric elements
        for elem in tree.iter():
            tag = elem.tag if isinstance(elem.tag, str) else ""

            is_nonfraction = tag == f"{{{ix_uri}}}nonFraction"
            is_nonnumeric = tag == f"{{{ix_uri}}}nonNumeric"

            if not is_nonfraction and not is_nonnumeric:
                continue

            name_attr = elem.get("name", "")
            context_ref = elem.get("contextRef", "")
            text = "".join(elem.itertext()).strip()

            if not name_attr or not text:
                continue

            local_name = name_attr.split(":")[-1] if ":" in name_attr else name_attr

            if is_nonfraction and _matches_ratio_pattern(local_name):
                try:
                    val = _parse_ix_number(elem, text)
                    if val is not None:
                        val = _normalize_ratio(val)
                        if _is_previous_ratio(local_name, context_ref):
                            if result["previous_holding_ratio"] is None:
                                result["previous_holding_ratio"] = val
                        else:
                            if result["holding_ratio"] is None:
                                result["holding_ratio"] = val
                except (ValueError, AttributeError):
                    continue

            elif is_nonfraction and _matches_shares_pattern(local_name):
                try:
                    val = _parse_ix_number(elem, text)
                    if val is not None:
                        if "Prior" not in context_ref and "Previous" not in context_ref:
                            if result["shares_held"] is None:
                                result["shares_held"] = int(val)
                except (ValueError, AttributeError):
                    continue

            elif is_nonnumeric:
                if _matches_holder_pattern(local_name, name_attr):
                    if not result["holder_name"]:
                        result["holder_name"] = text
                elif _matches_target_pattern(local_name):
                    if not result["target_company_name"]:
                        result["target_company_name"] = text
                elif _matches_sec_code_pattern(local_name):
                    if not result["target_sec_code"]:
                        result["target_sec_code"] = text
                elif _matches_purpose_pattern(local_name):
                    if not result["purpose_of_holding"]:
                        result["purpose_of_holding"] = text

        return result

    def _extract_inline_via_regex(self, htm_bytes: bytes) -> dict:
        """Extract inline XBRL data using regex (fallback when parsers fail)."""
        result = _empty_holding_result()

        text = htm_bytes.decode("utf-8", errors="replace")

        # Match ix:nonFraction elements with name and contextRef
        # Pattern: <ix:nonFraction ... name="prefix:ElementName" ... contextRef="xxx" ...>value</ix:nonFraction>
        nonfrac_pat = re.compile(
            r'<[^>]*?:nonFraction[^>]*?'
            r'name=["\']([^"\']+)["\'][^>]*?'
            r'contextRef=["\']([^"\']+)["\']'
            r'[^>]*?>(.*?)</[^>]*?:nonFraction>',
            re.DOTALL | re.IGNORECASE,
        )
        # Also match when contextRef comes before name
        nonfrac_pat2 = re.compile(
            r'<[^>]*?:nonFraction[^>]*?'
            r'contextRef=["\']([^"\']+)["\'][^>]*?'
            r'name=["\']([^"\']+)["\']'
            r'[^>]*?>(.*?)</[^>]*?:nonFraction>',
            re.DOTALL | re.IGNORECASE,
        )

        for m in nonfrac_pat.finditer(text):
            name_attr, ctx, val_text = m.group(1), m.group(2), m.group(3)
            self._apply_nonfraction_regex(result, name_attr, ctx, val_text)

        for m in nonfrac_pat2.finditer(text):
            ctx, name_attr, val_text = m.group(1), m.group(2), m.group(3)
            self._apply_nonfraction_regex(result, name_attr, ctx, val_text)

        # Match ix:nonNumeric elements
        nonnumeric_pat = re.compile(
            r'<[^>]*?:nonNumeric[^>]*?'
            r'name=["\']([^"\']+)["\']'
            r'[^>]*?>(.*?)</[^>]*?:nonNumeric>',
            re.DOTALL | re.IGNORECASE,
        )

        for m in nonnumeric_pat.finditer(text):
            name_attr, val_text = m.group(1), m.group(2)
            # Strip HTML tags from value
            clean_val = re.sub(r'<[^>]+>', '', val_text).strip()
            if not clean_val:
                continue

            local_name = name_attr.split(":")[-1]

            if _matches_holder_pattern(local_name, name_attr):
                if not result["holder_name"]:
                    result["holder_name"] = clean_val
            elif _matches_target_pattern(local_name):
                if not result["target_company_name"]:
                    result["target_company_name"] = clean_val
            elif _matches_sec_code_pattern(local_name):
                if not result["target_sec_code"]:
                    result["target_sec_code"] = clean_val
            elif _matches_purpose_pattern(local_name):
                if not result["purpose_of_holding"]:
                    result["purpose_of_holding"] = clean_val

        return result

    def _apply_nonfraction_regex(self, result: dict, name_attr: str, ctx: str, val_text: str):
        """Apply a regex-matched nonFraction value to the result dict.

        正規表現フォールバックで抽出した ix:nonFraction の値を、
        要素名とcontextRefから is_previous 判定して result に格納する。
        """
        local_name = name_attr.split(":")[-1]
        # Strip HTML tags
        clean_val = re.sub(r'<[^>]+>', '', val_text).strip()
        # Extract scale from the tag (regex can't easily get attributes, assume no scale)
        cleaned = re.sub(r'[,、\s　株%％]', '', clean_val)
        if not cleaned or cleaned in ('-', '―'):
            return

        try:
            val = float(cleaned)
        except ValueError:
            return

        if _matches_ratio_pattern(local_name):
            val = _normalize_ratio(val)
            if _is_previous_ratio(local_name, ctx):
                if result["previous_holding_ratio"] is None:
                    result["previous_holding_ratio"] = val
            else:
                if result["holding_ratio"] is None:
                    result["holding_ratio"] = val

        elif _matches_shares_pattern(local_name):
            if "Prior" not in ctx and "Previous" not in ctx:
                if result["shares_held"] is None:
                    result["shares_held"] = int(val)

    def diagnose_xbrl(self, zip_content: bytes) -> dict:
        """Diagnostic: return detailed info about XBRL ZIP contents and parsing.

        Used by the debug endpoint to troubleshoot XBRL parsing issues.
        """
        info = {
            "zip_valid": False,
            "files": [],
            "xbrl_files": [],
            "htm_files": [],
            "xbrl_sample_elements": [],
            "htm_sample_elements": [],
            "parse_result": None,
        }

        try:
            with zipfile.ZipFile(io.BytesIO(zip_content)) as zf:
                info["zip_valid"] = True
                all_files = zf.namelist()
                info["files"] = all_files

                # Use same file discovery as parse_xbrl_for_holding_data
                xbrl_pub = [f for f in all_files
                            if f.endswith(".xbrl") and "PublicDoc" in f]
                xbrl_any = [f for f in all_files
                            if f.endswith(".xbrl")
                            and "AuditDoc" not in f
                            and "__MACOSX" not in f]
                info["xbrl_files"] = xbrl_pub or xbrl_any

                htm_pub = [f for f in all_files
                           if "PublicDoc" in f
                           and (f.endswith(".htm") or f.endswith(".xhtml"))]
                htm_any = [f for f in all_files
                           if (f.endswith(".htm") or f.endswith(".xhtml"))
                           and "AuditDoc" not in f
                           and "__MACOSX" not in f]
                info["htm_files"] = htm_pub or htm_any

                # Sample elements from .xbrl files
                for xf in info["xbrl_files"][:1]:
                    try:
                        tree = etree.fromstring(zf.read(xf))
                        elements = []
                        for elem in tree.iter():
                            local = elem.xpath("local-name()")
                            if any(kw in local for kw in (
                                "Shareholding", "Ratio", "Issuer", "Holder",
                                "Filer", "Security", "Share", "Purpose",
                            )):
                                elements.append({
                                    "tag": local,
                                    "text": (elem.text or "")[:100],
                                    "contextRef": elem.get("contextRef", ""),
                                })
                        info["xbrl_sample_elements"] = elements[:50]
                    except Exception as e:
                        info["xbrl_sample_elements"] = [{"error": str(e)}]

                # Sample elements from .htm files (inline XBRL)
                # Use XML parser (same as _extract_inline_via_xml) to preserve namespaces
                for hf in info["htm_files"][:1]:
                    try:
                        htm_bytes = zf.read(hf)
                        tree = etree.fromstring(htm_bytes)
                        elements = []
                        for elem in tree.iter():
                            tag = elem.tag if isinstance(elem.tag, str) else ""
                            # Check for ix:nonFraction / ix:nonNumeric via namespace URI
                            if "inlineXBRL" in tag:
                                local_tag = tag.rsplit("}", 1)[-1] if "}" in tag else tag
                                name = elem.get("name", "")
                                text = "".join(elem.itertext()).strip()
                                elements.append({
                                    "tag": local_tag,
                                    "name": name,
                                    "text": text[:200],
                                    "contextRef": elem.get("contextRef", ""),
                                    "format": elem.get("format", ""),
                                    "scale": elem.get("scale", ""),
                                })
                        info["htm_sample_elements"] = elements[:80]
                    except etree.XMLSyntaxError:
                        # Fallback: use regex to show what's in the file
                        text = htm_bytes.decode("utf-8", errors="replace")
                        nf_matches = re.findall(
                            r'<[^>]*:nonFraction[^>]*name=["\']([^"\']+)["\'][^>]*>(.*?)</[^>]*:nonFraction>',
                            text, re.DOTALL,
                        )
                        nn_matches = re.findall(
                            r'<[^>]*:nonNumeric[^>]*name=["\']([^"\']+)["\'][^>]*>(.*?)</[^>]*:nonNumeric>',
                            text, re.DOTALL,
                        )
                        elements = []
                        for name, val in nf_matches:
                            elements.append({"tag": "nonFraction(regex)", "name": name, "text": val.strip()[:200]})
                        for name, val in nn_matches:
                            elements.append({"tag": "nonNumeric(regex)", "name": name, "text": val.strip()[:200]})
                        info["htm_sample_elements"] = elements[:80]
                    except Exception as e:
                        info["htm_sample_elements"] = [{"error": str(e)}]

                # Run actual parse
                info["parse_result"] = self.parse_xbrl_for_holding_data(zip_content)

        except zipfile.BadZipFile:
            info["zip_valid"] = False
        except Exception as e:
            info["error"] = str(e)

        return info

    # ------------------------------------------------------------------
    # 有価証券報告書 / 四半期報告書 parsing for company fundamentals
    # ------------------------------------------------------------------

    async def fetch_all_document_list(self, target_date: date) -> list[dict]:
        """Fetch ALL document types for a date (not just large shareholding).

        Used to discover 有価証券報告書 (120), 四半期報告書 (140), etc.
        for company fundamental data extraction.
        """
        return await self._fetch_documents_raw(target_date)

    def parse_xbrl_for_company_info(self, zip_content: bytes) -> dict:
        """Parse 有価証券報告書 / 四半期報告書 XBRL for company fundamentals.

        Extracts:
        - shares_outstanding: 発行済株式数
        - net_assets: 純資産
        - company_name: 会社名

        These values come from the official financial statements submitted
        to 金融庁 via EDINET — the most authoritative source.
        """
        result = {
            "shares_outstanding": None,
            "net_assets": None,
            "company_name": None,
        }

        try:
            with zipfile.ZipFile(io.BytesIO(zip_content)) as zf:
                xbrl_files = [
                    f for f in zf.namelist()
                    if f.endswith(".xbrl") and "PublicDoc" in f
                ]
                if not xbrl_files:
                    return result

                xbrl_content = zf.read(xbrl_files[0])
                result = self._extract_company_info(xbrl_content)
        except zipfile.BadZipFile:
            logger.warning("Invalid ZIP for company info parsing")
        except Exception as e:
            logger.error("Company info XBRL parsing error: %s", e)

        return result

    def _extract_company_info(self, xbrl_bytes: bytes) -> dict:
        """Extract company fundamentals from 有報/四半期 XBRL."""
        result = {
            "shares_outstanding": None,
            "net_assets": None,
            "company_name": None,
        }

        try:
            tree = etree.fromstring(xbrl_bytes)
        except etree.XMLSyntaxError as e:
            logger.warning("XBRL XML parse error: %s", e)
            return result

        # --- 発行済株式数 (Shares Outstanding) ---
        # XBRL elements in 有報: NumberOfIssuedSharesXxx, TotalNumberOfIssuedShares
        shares_patterns = [
            "NumberOfIssuedSharesTotalNumberOfSharesEtcRegularShares",
            "TotalNumberOfIssuedShares",
            "NumberOfIssuedShares",
            "IssuedSharesTotalNumber",
        ]
        for pattern in shares_patterns:
            elements = tree.xpath(
                f"//*[contains(local-name(), '{pattern}')]"
            )
            for elem in elements:
                try:
                    val = int(float(elem.text.strip()))
                    context_ref = elem.get("contextRef", "")
                    # Take "Current" / "Instant" context, skip "Prior"
                    if "Prior" not in context_ref and "Previous" not in context_ref:
                        if result["shares_outstanding"] is None or val > result["shares_outstanding"]:
                            result["shares_outstanding"] = val
                except (ValueError, AttributeError):
                    continue
            if result["shares_outstanding"] is not None:
                break

        # --- 純資産 (Net Assets / Total Equity) ---
        equity_patterns = [
            "NetAssets",
            "EquityAttributableToOwnersOfParent",
            "TotalEquity",
            "ShareholdersEquity",
        ]
        for pattern in equity_patterns:
            elements = tree.xpath(
                f"//*[contains(local-name(), '{pattern}')]"
            )
            for elem in elements:
                try:
                    val = int(float(elem.text.strip()))
                    context_ref = elem.get("contextRef", "")
                    if "Prior" not in context_ref and "Previous" not in context_ref:
                        if result["net_assets"] is None:
                            result["net_assets"] = val
                            break
                except (ValueError, AttributeError):
                    continue
            if result["net_assets"] is not None:
                break

        # --- 会社名 (Company Name) ---
        name_patterns = [
            "CompanyName",
            "FilerName",
        ]
        for pattern in name_patterns:
            elements = tree.xpath(
                f"//*[contains(local-name(), '{pattern}')]"
            )
            for elem in elements:
                if elem.text and elem.text.strip():
                    result["company_name"] = elem.text.strip()
                    break
            if result["company_name"]:
                break

        logger.debug("Extracted company info: %s", result)
        return result


# ---------------------------------------------------------------------------
# Helper functions for inline XBRL element name matching
# ---------------------------------------------------------------------------

def _name_contains_any(name: str, patterns: tuple[str, ...], exclude: tuple[str, ...] = ()) -> bool:
    """Check if name contains any of the patterns and none of the exclusions."""
    if exclude and any(skip in name for skip in exclude):
        return False
    return any(p in name for p in patterns)

_RATIO_PATTERNS = ("HoldingRatio", "ShareholdingRatio", "RatioOfShareholdingToTotalIssuedShares", "RatioOfShareCertificatesEtc")
_RATIO_EXCLUDE = ("Abstract", "EachLargeShareholder", "JointHolder")
_SHARES_PATTERNS = ("TotalNumberOfShareCertificatesEtcHeld", "TotalNumberOfSharesHeld", "NumberOfShareCertificatesEtc", "NumberOfStocksEtc")
_HOLDER_PATTERNS = ("NameOfLargeShareholdingReporter", "NameOfFiler", "ReporterName", "LargeShareholderName")
_TARGET_PATTERNS = ("IssuerNameLargeShareholding", "IssuerName", "NameOfIssuer", "TargetCompanyName")
_SEC_CODE_PATTERNS = ("SecurityCodeOfIssuer", "IssuerSecuritiesCode", "SecurityCode")
_PURPOSE_PATTERNS = ("PurposeOfHolding",)

def _matches_ratio_pattern(name: str) -> bool:
    return _name_contains_any(name, _RATIO_PATTERNS, _RATIO_EXCLUDE)

def _matches_shares_pattern(name: str) -> bool:
    return _name_contains_any(name, _SHARES_PATTERNS, ("Abstract",))

def _matches_holder_pattern(name: str, full_qname: str = "") -> bool:
    if _name_contains_any(name, _HOLDER_PATTERNS):
        return True
    return name == "Name" and ("jplvh" in full_qname or "lvh" in full_qname)

def _matches_target_pattern(name: str) -> bool:
    return _name_contains_any(name, _TARGET_PATTERNS)

def _matches_sec_code_pattern(name: str) -> bool:
    return _name_contains_any(name, _SEC_CODE_PATTERNS)

def _matches_purpose_pattern(name: str) -> bool:
    return _name_contains_any(name, _PURPOSE_PATTERNS)


def _parse_ix_number(elem, text: str) -> float | None:
    """Parse a numeric value from an inline XBRL element.

    Handles:
    - scale attribute (e.g. scale="6" means multiply by 10^6)
    - sign attribute (e.g. sign="-")
    - format attribute (number formatting with commas)
    - Japanese number formats
    """
    # Clean text: remove commas, spaces, Japanese characters
    cleaned = re.sub(r"[,、\s　株%％]", "", text)
    if not cleaned or cleaned == "-" or cleaned == "―":
        return None

    try:
        val = float(cleaned)
    except ValueError:
        return None

    # Apply scale attribute (e.g. scale="6" means * 10^6)
    scale = elem.get("scale", "")
    if scale:
        try:
            val *= 10 ** int(scale)
        except ValueError:
            pass

    # Apply sign
    sign = elem.get("sign", "")
    if sign == "-":
        val = -val

    return val


# Singleton client
edinet_client = EdinetClient()
