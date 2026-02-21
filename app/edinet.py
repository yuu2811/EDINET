"""EDINET API v2 client for fetching large shareholding reports."""

import io
import logging
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

    async def fetch_document_list(
        self, target_date: date
    ) -> list[dict]:
        """Fetch the document list for a given date from EDINET API.

        Returns only large shareholding reports (docTypeCode 350/360).
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

        results = data.get("results", [])
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

    async def download_xbrl(self, doc_id: str) -> bytes | None:
        """Download the XBRL ZIP for a given document ID."""
        client = await self._get_client()
        url = f"{self.base_url}/documents/{doc_id}"
        params = {
            "type": 1,  # XBRL data
            "Subscription-Key": self.api_key,
        }

        try:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            return resp.content
        except Exception as e:
            logger.error("Failed to download XBRL for %s: %s", doc_id, e)
            return None

    async def download_pdf(self, doc_id: str) -> bytes | None:
        """Download the PDF for a given document ID (type=2).

        API v2 returns a ZIP (application/octet-stream) containing PDF files.
        This method extracts the first PDF found in the ZIP and returns it.
        """
        client = await self._get_client()
        url = f"{self.base_url}/documents/{doc_id}"
        params = {
            "type": 2,  # PDF
            "Subscription-Key": self.api_key,
        }

        try:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
        except Exception as e:
            logger.error("Failed to download PDF for %s: %s", doc_id, e)
            return None

        try:
            with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
                pdf_files = [
                    f for f in zf.namelist()
                    if f.lower().endswith(".pdf")
                ]
                if not pdf_files:
                    logger.warning("No PDF files found in ZIP for %s", doc_id)
                    return None
                return zf.read(pdf_files[0])
        except zipfile.BadZipFile:
            # Some older docs may return raw PDF directly
            if resp.content[:5].startswith(b"%PDF"):
                return resp.content
            logger.warning(
                "EDINET returned neither valid ZIP nor PDF for %s (%d bytes)",
                doc_id, len(resp.content),
            )
            return None

    async def download_csv(self, doc_id: str) -> bytes | None:
        """Download the CSV data for a given document ID (type=5).

        API v2 new feature: XBRL data converted to CSV format.
        Response is a ZIP file (application/octet-stream) containing CSV files
        in XBRL_TO_CSV/ directory.

        Returns None if CSV is not available for this document.
        """
        client = await self._get_client()
        url = f"{self.base_url}/documents/{doc_id}"
        params = {
            "type": 5,  # CSV data (API v2 new)
            "Subscription-Key": self.api_key,
        }

        try:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            return resp.content
        except Exception as e:
            logger.debug("CSV download failed for %s: %s", doc_id, e)
            return None

    def parse_csv_for_holding_data(self, zip_content: bytes) -> dict:
        """Parse CSV ZIP (type=5) to extract shareholding data.

        API v2 provides XBRL data converted to CSV, which is simpler to parse
        than raw XBRL XML.  The CSV files are in XBRL_TO_CSV/ directory.

        Returns the same dict structure as parse_xbrl_for_holding_data.
        """
        import csv as _csv

        result = {
            "holding_ratio": None,
            "previous_holding_ratio": None,
            "holder_name": None,
            "target_company_name": None,
            "target_sec_code": None,
            "shares_held": None,
            "purpose_of_holding": None,
        }

        try:
            with zipfile.ZipFile(io.BytesIO(zip_content)) as zf:
                csv_files = [
                    f for f in zf.namelist()
                    if f.endswith(".csv") and "XBRL_TO_CSV" in f
                ]
                if not csv_files:
                    return result

                for csv_path in csv_files:
                    raw = zf.read(csv_path)
                    # CSV encoding: try cp932 first (common for JP filings)
                    for enc in ("cp932", "utf-8-sig", "utf-8"):
                        try:
                            text = raw.decode(enc)
                            break
                        except UnicodeDecodeError:
                            continue
                    else:
                        text = raw.decode("utf-8", errors="replace")

                    reader = _csv.DictReader(io.StringIO(text))
                    for row in reader:
                        # Column names vary but typically include
                        # "要素ID" or "element_id" and "値" or "value"
                        elem_id = (
                            row.get("要素ID", "")
                            or row.get("element_id", "")
                            or row.get("ElementId", "")
                        ).lower()
                        value = (
                            row.get("値", "")
                            or row.get("value", "")
                            or row.get("Value", "")
                        ).strip()
                        context = (
                            row.get("コンテキストID", "")
                            or row.get("contextRef", "")
                            or row.get("ContextId", "")
                        )

                        if not elem_id or not value:
                            continue

                        if "shareholdingratio" in elem_id and "total" in elem_id:
                            # Skip abstract and individual holder entries
                            if "abstract" in elem_id or "eachlargeshareholder" in elem_id:
                                continue
                            try:
                                val = float(value)
                                # Auto-detect decimal vs percentage
                                if 0 < val < 1.0:
                                    val = round(val * 100, 4)
                                if "prior" in context.lower() or "previous" in context.lower():
                                    if result["previous_holding_ratio"] is None:
                                        result["previous_holding_ratio"] = val
                                elif result["holding_ratio"] is None:
                                    result["holding_ratio"] = val
                            except ValueError:
                                pass
                        elif "issuername" in elem_id and result["target_company_name"] is None:
                            result["target_company_name"] = value
                        elif "securitycode" in elem_id and result["target_sec_code"] is None:
                            result["target_sec_code"] = value
                        elif "nameoffiler" in elem_id or "largeshareholdingreporter" in elem_id:
                            if result["holder_name"] is None:
                                result["holder_name"] = value
                        elif "totalnumberofsharecertificates" in elem_id or "totalnumberofshares" in elem_id:
                            if "prior" not in context.lower() and "previous" not in context.lower():
                                try:
                                    result["shares_held"] = int(float(value))
                                except ValueError:
                                    pass
                        elif "purposeofholding" in elem_id and result["purpose_of_holding"] is None:
                            result["purpose_of_holding"] = value

        except zipfile.BadZipFile:
            logger.warning("Invalid ZIP file for CSV download")
        except Exception as e:
            logger.error("CSV parsing error: %s", e)

        return result

    def parse_xbrl_for_holding_data(self, zip_content: bytes) -> dict:
        """Parse XBRL ZIP to extract shareholding data.

        Returns a dict with keys:
        - holding_ratio: float | None
        - previous_holding_ratio: float | None
        - holder_name: str | None
        - target_company_name: str | None
        - target_sec_code: str | None
        - shares_held: int | None
        - purpose_of_holding: str | None
        """
        result = {
            "holding_ratio": None,
            "previous_holding_ratio": None,
            "holder_name": None,
            "target_company_name": None,
            "target_sec_code": None,
            "shares_held": None,
            "purpose_of_holding": None,
        }

        try:
            with zipfile.ZipFile(io.BytesIO(zip_content)) as zf:
                xbrl_files = [
                    f
                    for f in zf.namelist()
                    if f.endswith(".xbrl") and "PublicDoc" in f
                ]

                if not xbrl_files:
                    logger.debug("No XBRL files found in ZIP")
                    return result

                # Parse the first (usually only) public XBRL instance
                xbrl_content = zf.read(xbrl_files[0])
                result = self._extract_from_xbrl(xbrl_content)

        except zipfile.BadZipFile:
            logger.warning("Invalid ZIP file received from EDINET")
        except Exception as e:
            logger.error("XBRL parsing error: %s", e)

        return result

    def _extract_from_xbrl(self, xbrl_bytes: bytes) -> dict:
        """Extract holding data from XBRL instance XML."""
        result = {
            "holding_ratio": None,
            "previous_holding_ratio": None,
            "holder_name": None,
            "target_company_name": None,
            "target_sec_code": None,
            "shares_held": None,
            "purpose_of_holding": None,
        }

        try:
            tree = etree.fromstring(xbrl_bytes)
        except etree.XMLSyntaxError as e:
            logger.warning("XBRL XML parse error: %s", e)
            return result

        # Use local-name() to match elements regardless of namespace prefix.
        # Large shareholding report XBRL uses jpcrp060_cor namespace.

        # --- Holding ratio (保有割合) ---
        # Look for elements containing "ShareholdingRatio".
        # Priority: "Total" ratio > generic ratio.  Exclude abstract elements
        # and individual shareholder entries (EachLargeShareholder1, etc.)
        # which would give incorrect (per-holder) values.
        ratio_patterns = [
            "TotalShareholdingRatioOfShareCertificatesEtc",
            "TotalShareholdingRatio",
            "RatioOfShareholdingToTotalIssuedShares",
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
                        val = float(elem.text.strip())
                        # Auto-detect decimal vs percentage format:
                        # EDINET stores as percentage (e.g. 5.23 = 5.23%)
                        # but some filings use decimal (e.g. 0.0523 = 5.23%)
                        if 0 < val < 1.0:
                            val = round(val * 100, 4)
                        context_ref = elem.get("contextRef", "")
                        # "Prior" contexts contain the previous ratio
                        if "Prior" in context_ref or "Previous" in context_ref:
                            if result["previous_holding_ratio"] is None:
                                result["previous_holding_ratio"] = val
                        else:
                            if result["holding_ratio"] is None:
                                result["holding_ratio"] = val
                    except (ValueError, AttributeError):
                        continue
                if result["holding_ratio"] is not None:
                    break

        # Fallback: broader search if specific patterns didn't match
        if result["holding_ratio"] is None:
            elements = tree.xpath(
                "//*[contains(local-name(), 'ShareholdingRatio')]"
            )
            for elem in elements:
                local = elem.xpath("local-name()")
                # Exclude abstract, individual holder, and joint holder entries
                if any(skip in local for skip in (
                    "Abstract", "EachLargeShareholder", "JointHolder",
                )):
                    continue
                try:
                    val = float(elem.text.strip())
                    if 0 < val < 1.0:
                        val = round(val * 100, 4)
                    context_ref = elem.get("contextRef", "")
                    if "Prior" in context_ref or "Previous" in context_ref:
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

    # ------------------------------------------------------------------
    # 有価証券報告書 / 四半期報告書 parsing for company fundamentals
    # ------------------------------------------------------------------

    async def fetch_all_document_list(self, target_date: date) -> list[dict]:
        """Fetch ALL document types for a date (not just large shareholding).

        Used to discover 有価証券報告書 (120), 四半期報告書 (140), etc.
        for company fundamental data extraction.
        """
        client = await self._get_client()
        url = f"{self.base_url}/documents.json"
        params = {
            "date": target_date.strftime("%Y-%m-%d"),
            "type": 2,
            "Subscription-Key": self.api_key,
        }

        try:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.error("EDINET API request failed: %s", e)
            return []

        metadata = data.get("metadata", {})
        status = metadata.get("status")
        if status != "200":
            return []

        return data.get("results", [])

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


# Singleton client
edinet_client = EdinetClient()
