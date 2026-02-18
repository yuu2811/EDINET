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
            if doc_type in settings.LARGE_HOLDING_DOC_TYPES:
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
        # Look for elements containing "ShareholdingRatio" or "保有割合"
        ratio_patterns = [
            "TotalShareholdingRatioOfShareCertificatesEtc",
            "TotalShareholdingRatio",
            "ShareholdingRatio",
            "RatioOfShareholdingToTotalIssuedShares",
        ]
        for pattern in ratio_patterns:
            elements = tree.xpath(
                f"//*[contains(local-name(), '{pattern}')]"
            )
            if elements:
                for elem in elements:
                    try:
                        val = float(elem.text.strip())
                        context_ref = elem.get("contextRef", "")
                        # "Prior" contexts typically contain previous ratio
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


# Singleton client
edinet_client = EdinetClient()
