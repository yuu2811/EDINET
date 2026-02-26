"""Tests for EDINET API client."""

import io
import zipfile
from datetime import date
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.edinet import EdinetClient


def _mock_response(status_code: int, **kwargs) -> httpx.Response:
    """Create an httpx.Response with a dummy request attached (required since httpx 0.28)."""
    resp = httpx.Response(status_code, **kwargs)
    resp._request = httpx.Request("GET", "https://test.example.com")
    return resp


def _make_xbrl_zip(xbrl_content: bytes) -> bytes:
    """Helper to create a ZIP file with XBRL content."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("XBRL/PublicDoc/report.xbrl", xbrl_content)
    return buf.getvalue()


SAMPLE_XBRL = """<?xml version="1.0" encoding="UTF-8"?>
<xbrli:xbrl
    xmlns:xbrli="http://www.xbrl.org/2003/instance"
    xmlns:jpcrp060_cor="http://disclosure.edinet-fsa.go.jp/taxonomy/jpcrp060/2023-12-01/jpcrp060_cor">

  <jpcrp060_cor:TotalShareholdingRatioOfShareCertificatesEtc contextRef="CurrentPeriod">6.25</jpcrp060_cor:TotalShareholdingRatioOfShareCertificatesEtc>
  <jpcrp060_cor:TotalShareholdingRatioOfShareCertificatesEtc contextRef="PriorPeriod">5.10</jpcrp060_cor:TotalShareholdingRatioOfShareCertificatesEtc>

  <jpcrp060_cor:NameOfLargeShareholdingReporter contextRef="CurrentPeriod">テストファンド株式会社</jpcrp060_cor:NameOfLargeShareholdingReporter>

  <jpcrp060_cor:IssuerNameLargeShareholding contextRef="CurrentPeriod">ターゲット産業株式会社</jpcrp060_cor:IssuerNameLargeShareholding>

  <jpcrp060_cor:SecurityCodeOfIssuer contextRef="CurrentPeriod">77770</jpcrp060_cor:SecurityCodeOfIssuer>

  <jpcrp060_cor:TotalNumberOfShareCertificatesEtcHeld contextRef="CurrentPeriod">5000000</jpcrp060_cor:TotalNumberOfShareCertificatesEtcHeld>

  <jpcrp060_cor:PurposeOfHoldingOfShareCertificatesEtc contextRef="CurrentPeriod">純投資</jpcrp060_cor:PurposeOfHoldingOfShareCertificatesEtc>

</xbrli:xbrl>
""".encode("utf-8")

SAMPLE_XBRL_MINIMAL = """<?xml version="1.0" encoding="UTF-8"?>
<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance"
    xmlns:jpcrp060_cor="http://disclosure.edinet-fsa.go.jp/taxonomy/jpcrp060/2023-12-01/jpcrp060_cor">
  <jpcrp060_cor:TotalShareholdingRatioOfShareCertificatesEtc contextRef="Current">8.50</jpcrp060_cor:TotalShareholdingRatioOfShareCertificatesEtc>
</xbrli:xbrl>
""".encode("utf-8")


class TestXBRLParsing:
    """Tests for XBRL parsing logic."""

    def setup_method(self):
        self.client = EdinetClient()

    def test_parse_full_xbrl(self):
        """Should extract all fields from a full XBRL document."""
        zip_data = _make_xbrl_zip(SAMPLE_XBRL)
        result = self.client.parse_xbrl_for_holding_data(zip_data)

        assert result["holding_ratio"] == pytest.approx(6.25)
        assert result["previous_holding_ratio"] == pytest.approx(5.10)
        assert result["holder_name"] == "テストファンド株式会社"
        assert result["target_company_name"] == "ターゲット産業株式会社"
        assert result["target_sec_code"] == "77770"
        assert result["shares_held"] == 5000000
        assert result["purpose_of_holding"] == "純投資"

    def test_parse_minimal_xbrl(self):
        """Should extract what's available from minimal XBRL."""
        zip_data = _make_xbrl_zip(SAMPLE_XBRL_MINIMAL)
        result = self.client.parse_xbrl_for_holding_data(zip_data)

        assert result["holding_ratio"] == pytest.approx(8.50)
        assert result["previous_holding_ratio"] is None
        assert result["holder_name"] is None
        assert result["target_company_name"] is None

    def test_parse_empty_zip(self):
        """Should return empty result for a ZIP with no XBRL files."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("readme.txt", "no xbrl here")
        result = self.client.parse_xbrl_for_holding_data(buf.getvalue())

        assert result["holding_ratio"] is None
        assert result["holder_name"] is None

    def test_parse_invalid_zip(self):
        """Should handle invalid ZIP data gracefully."""
        result = self.client.parse_xbrl_for_holding_data(b"not a zip file")
        assert result["holding_ratio"] is None

    def test_parse_malformed_xbrl(self):
        """Should handle malformed XML gracefully."""
        zip_data = _make_xbrl_zip(b"<not valid xml><<<")
        result = self.client.parse_xbrl_for_holding_data(zip_data)
        assert result["holding_ratio"] is None

    def test_parse_xbrl_non_numeric_ratio(self):
        """Should handle non-numeric ratio values."""
        xbrl = b"""<?xml version="1.0" encoding="UTF-8"?>
        <xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance"
            xmlns:ns="http://example.com/ns">
          <ns:TotalShareholdingRatio contextRef="Current">N/A</ns:TotalShareholdingRatio>
        </xbrli:xbrl>"""
        zip_data = _make_xbrl_zip(xbrl)
        result = self.client.parse_xbrl_for_holding_data(zip_data)
        assert result["holding_ratio"] is None


class TestFetchDocumentList:
    """Tests for the document list API call."""

    def setup_method(self):
        self.client = EdinetClient()

    @pytest.mark.asyncio
    async def test_fetch_filters_large_holdings(self):
        """Should filter only docTypeCode 350/360 from results."""
        from tests.conftest import SAMPLE_EDINET_RESPONSE

        mock_response = _mock_response(200, json=SAMPLE_EDINET_RESPONSE)

        with patch.object(self.client, "_get_client") as mock_get:
            mock_http = AsyncMock()
            mock_http.get = AsyncMock(return_value=mock_response)
            mock_get.return_value = mock_http

            result = await self.client.fetch_document_list(date(2026, 2, 18))

        # Only 2 out of 3 documents should pass (docTypeCode 350)
        assert len(result) == 2
        assert all(r["docTypeCode"] == "350" for r in result)
        # The 有価証券報告書 (docTypeCode 120) should be filtered out
        assert not any(r["docID"] == "S100ABC3" for r in result)

    @pytest.mark.asyncio
    async def test_fetch_handles_api_error(self):
        """Should return empty list on API HTTP error."""
        mock_response = _mock_response(500)

        with patch.object(self.client, "_get_client") as mock_get:
            mock_http = AsyncMock()
            mock_http.get = AsyncMock(return_value=mock_response)
            mock_get.return_value = mock_http

            result = await self.client.fetch_document_list(date(2026, 2, 18))

        assert result == []

    @pytest.mark.asyncio
    async def test_fetch_handles_non_200_status(self):
        """Should return empty list when API returns non-200 status in body."""
        error_resp = {
            "metadata": {"status": "404", "message": "Not Found"},
            "results": [],
        }
        mock_response = _mock_response(200, json=error_resp)

        with patch.object(self.client, "_get_client") as mock_get:
            mock_http = AsyncMock()
            mock_http.get = AsyncMock(return_value=mock_response)
            mock_get.return_value = mock_http

            result = await self.client.fetch_document_list(date(2026, 2, 18))

        assert result == []

    @pytest.mark.asyncio
    async def test_fetch_handles_network_error(self):
        """Should return empty list on network error."""
        with patch.object(self.client, "_get_client") as mock_get:
            mock_http = AsyncMock()
            mock_http.get = AsyncMock(side_effect=httpx.ConnectError("connection refused"))
            mock_get.return_value = mock_http

            result = await self.client.fetch_document_list(date(2026, 2, 18))

        assert result == []

    @pytest.mark.asyncio
    async def test_fetch_empty_results(self):
        """Should return empty list when no results."""
        resp = {
            "metadata": {"status": "200", "message": "OK"},
            "results": [],
        }
        mock_response = _mock_response(200, json=resp)

        with patch.object(self.client, "_get_client") as mock_get:
            mock_http = AsyncMock()
            mock_http.get = AsyncMock(return_value=mock_response)
            mock_get.return_value = mock_http

            result = await self.client.fetch_document_list(date(2026, 2, 18))

        assert result == []


class TestDownloadXBRL:
    """Tests for XBRL document download."""

    def setup_method(self):
        self.client = EdinetClient()

    @pytest.mark.asyncio
    async def test_download_success(self):
        """Should return bytes on successful download."""
        zip_data = _make_xbrl_zip(SAMPLE_XBRL)
        mock_response = _mock_response(200, content=zip_data)

        with patch.object(self.client, "_get_client") as mock_get:
            mock_http = AsyncMock()
            mock_http.get = AsyncMock(return_value=mock_response)
            mock_get.return_value = mock_http

            result = await self.client.download_xbrl("S100TEST")

        assert result == zip_data

    @pytest.mark.asyncio
    async def test_download_failure(self):
        """Should return None on download failure."""
        with patch.object(self.client, "_get_client") as mock_get:
            mock_http = AsyncMock()
            mock_http.get = AsyncMock(side_effect=httpx.ConnectError("fail"))
            mock_get.return_value = mock_http

            result = await self.client.download_xbrl("S100TEST")

        assert result is None


def _make_pdf_zip(pdf_content: bytes) -> bytes:
    """Helper to create a ZIP file containing a PDF."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("S100TEST/report.pdf", pdf_content)
    return buf.getvalue()


class TestDownloadPDF:
    """Tests for PDF document download (ZIP extraction)."""

    def setup_method(self):
        self.client = EdinetClient()

    @pytest.mark.asyncio
    async def test_download_pdf_extracts_from_zip(self):
        """Should extract PDF from the ZIP returned by EDINET API."""
        fake_pdf = b"%PDF-1.4 test pdf content"
        zip_data = _make_pdf_zip(fake_pdf)
        mock_response = _mock_response(200, content=zip_data)

        with patch.object(self.client, "_get_client") as mock_get:
            mock_http = AsyncMock()
            mock_http.get = AsyncMock(return_value=mock_response)
            mock_get.return_value = mock_http

            result = await self.client.download_pdf("S100TEST")

        assert result == fake_pdf

    @pytest.mark.asyncio
    async def test_download_pdf_no_pdf_in_zip(self):
        """Should return None when ZIP has no PDF files."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("readme.txt", "no pdf here")
        mock_response = _mock_response(200, content=buf.getvalue())

        with patch.object(self.client, "_get_client") as mock_get:
            mock_http = AsyncMock()
            mock_http.get = AsyncMock(return_value=mock_response)
            mock_get.return_value = mock_http

            result = await self.client.download_pdf("S100TEST")

        assert result is None

    @pytest.mark.asyncio
    async def test_download_pdf_raw_pdf_fallback(self):
        """Should return raw PDF if response is not a ZIP (older API behavior)."""
        raw_pdf = b"%PDF-1.4 raw pdf content"
        mock_response = _mock_response(200, content=raw_pdf)

        with patch.object(self.client, "_get_client") as mock_get:
            mock_http = AsyncMock()
            mock_http.get = AsyncMock(return_value=mock_response)
            mock_get.return_value = mock_http

            result = await self.client.download_pdf("S100TEST")

        assert result == raw_pdf


    @pytest.mark.asyncio
    async def test_download_pdf_raw_pdf_with_leading_whitespace(self):
        """Should accept raw PDF even if upstream prepends whitespace/BOM."""
        raw_pdf = b"\xef\xbb\xbf\n%PDF-1.4 raw pdf content"
        mock_response = _mock_response(200, content=raw_pdf)

        with patch.object(self.client, "_get_client") as mock_get:
            mock_http = AsyncMock()
            mock_http.get = AsyncMock(return_value=mock_response)
            mock_get.return_value = mock_http

            result = await self.client.download_pdf("S100TEST")

        assert result == raw_pdf

    @pytest.mark.asyncio
    async def test_download_pdf_non_zip_non_pdf(self):
        """Should return None for non-ZIP, non-PDF responses (e.g. HTML error)."""
        html_error = b"<html><body>Error</body></html>"
        mock_response = _mock_response(200, content=html_error)

        with patch.object(self.client, "_get_client") as mock_get:
            mock_http = AsyncMock()
            mock_http.get = AsyncMock(return_value=mock_response)
            mock_get.return_value = mock_http

            result = await self.client.download_pdf("S100TEST")

        assert result is None

    @pytest.mark.asyncio
    async def test_download_pdf_json_error_response(self):
        """Should detect JSON error responses from EDINET API (HTTP 200 with JSON body)."""
        error_json = b'{"metadata":{"status":"404","message":"not found"}}'
        mock_response = _mock_response(
            200,
            content=error_json,
            headers={"content-type": "application/json; charset=utf-8"},
        )

        with patch.object(self.client, "_get_client") as mock_get:
            mock_http = AsyncMock()
            mock_http.get = AsyncMock(return_value=mock_response)
            mock_get.return_value = mock_http

            result = await self.client.download_pdf("S100TEST")

        assert result is None

    @pytest.mark.asyncio
    async def test_download_pdf_html_error_response(self):
        """Should detect HTML error pages from EDINET API."""
        html_page = b"<html><body>Service Unavailable</body></html>"
        mock_response = _mock_response(
            200,
            content=html_page,
            headers={"content-type": "text/html; charset=utf-8"},
        )

        with patch.object(self.client, "_get_client") as mock_get:
            mock_http = AsyncMock()
            mock_http.get = AsyncMock(return_value=mock_response)
            mock_get.return_value = mock_http

            result = await self.client.download_pdf("S100TEST")

        assert result is None

    @pytest.mark.asyncio
    async def test_download_pdf_http_error(self):
        """Should return None on HTTP error status."""
        mock_response = _mock_response(404)

        with patch.object(self.client, "_get_client") as mock_get:
            mock_http = AsyncMock()
            mock_http.get = AsyncMock(return_value=mock_response)
            mock_get.return_value = mock_http

            result = await self.client.download_pdf("S100TEST")

        assert result is None

    @pytest.mark.asyncio
    async def test_download_pdf_network_error(self):
        """Should return None on network error."""
        with patch.object(self.client, "_get_client") as mock_get:
            mock_http = AsyncMock()
            mock_http.get = AsyncMock(side_effect=httpx.ConnectError("fail"))
            mock_get.return_value = mock_http

            result = await self.client.download_pdf("S100TEST")

        assert result is None


class TestJointHolderExtraction:
    """Tests for joint holder extraction from XBRL."""

    def setup_method(self):
        self.client = EdinetClient()

    def test_extract_joint_holders_from_xbrl(self):
        """Should extract joint holder names from traditional XBRL."""
        xbrl = """<?xml version="1.0" encoding="UTF-8"?>
        <xbrli:xbrl
            xmlns:xbrli="http://www.xbrl.org/2003/instance"
            xmlns:jpcrp060_cor="http://disclosure.edinet-fsa.go.jp/taxonomy/jpcrp060/2023-12-01/jpcrp060_cor">
          <jpcrp060_cor:TotalShareholdingRatioOfShareCertificatesEtc contextRef="Current">7.50</jpcrp060_cor:TotalShareholdingRatioOfShareCertificatesEtc>
          <jpcrp060_cor:JointHolder1Name contextRef="Current">共同保有者A株式会社</jpcrp060_cor:JointHolder1Name>
          <jpcrp060_cor:JointHolder2Name contextRef="Current">共同保有者B株式会社</jpcrp060_cor:JointHolder2Name>
          <jpcrp060_cor:JointHolder1HoldingRatio contextRef="Current">3.20</jpcrp060_cor:JointHolder1HoldingRatio>
          <jpcrp060_cor:JointHolder2HoldingRatio contextRef="Current">2.10</jpcrp060_cor:JointHolder2HoldingRatio>
        </xbrli:xbrl>"""
        zip_data = _make_xbrl_zip(xbrl.encode("utf-8"))
        result = self.client.parse_xbrl_for_holding_data(zip_data)
        assert result["joint_holders"] is not None
        import json
        jh = json.loads(result["joint_holders"])
        assert len(jh) == 2
        assert jh[0]["name"] == "共同保有者A株式会社"
        assert jh[0]["ratio"] == 3.20
        assert jh[1]["name"] == "共同保有者B株式会社"
        assert jh[1]["ratio"] == 2.10

    def test_no_joint_holders(self):
        """Should return None when no joint holders in XBRL."""
        xbrl = """<?xml version="1.0" encoding="UTF-8"?>
        <xbrli:xbrl
            xmlns:xbrli="http://www.xbrl.org/2003/instance"
            xmlns:jpcrp060_cor="http://disclosure.edinet-fsa.go.jp/taxonomy/jpcrp060/2023-12-01/jpcrp060_cor">
          <jpcrp060_cor:TotalShareholdingRatioOfShareCertificatesEtc contextRef="Current">5.00</jpcrp060_cor:TotalShareholdingRatioOfShareCertificatesEtc>
        </xbrli:xbrl>"""
        zip_data = _make_xbrl_zip(xbrl.encode("utf-8"))
        result = self.client.parse_xbrl_for_holding_data(zip_data)
        assert result["joint_holders"] is None


class TestFundSourceExtraction:
    """Tests for acquisition fund source extraction from XBRL."""

    def setup_method(self):
        self.client = EdinetClient()

    def test_extract_fund_source(self):
        """Should extract fund source from traditional XBRL."""
        xbrl = """<?xml version="1.0" encoding="UTF-8"?>
        <xbrli:xbrl
            xmlns:xbrli="http://www.xbrl.org/2003/instance"
            xmlns:jpcrp060_cor="http://disclosure.edinet-fsa.go.jp/taxonomy/jpcrp060/2023-12-01/jpcrp060_cor">
          <jpcrp060_cor:TotalShareholdingRatioOfShareCertificatesEtc contextRef="Current">8.00</jpcrp060_cor:TotalShareholdingRatioOfShareCertificatesEtc>
          <jpcrp060_cor:DescriptionOfFundsForAcquisition contextRef="Current">自己資金及び借入金</jpcrp060_cor:DescriptionOfFundsForAcquisition>
        </xbrli:xbrl>"""
        zip_data = _make_xbrl_zip(xbrl.encode("utf-8"))
        result = self.client.parse_xbrl_for_holding_data(zip_data)
        assert result["fund_source"] == "自己資金及び借入金"

    def test_no_fund_source(self):
        """Should return None when no fund source in XBRL."""
        xbrl = """<?xml version="1.0" encoding="UTF-8"?>
        <xbrli:xbrl
            xmlns:xbrli="http://www.xbrl.org/2003/instance"
            xmlns:jpcrp060_cor="http://disclosure.edinet-fsa.go.jp/taxonomy/jpcrp060/2023-12-01/jpcrp060_cor">
          <jpcrp060_cor:TotalShareholdingRatioOfShareCertificatesEtc contextRef="Current">5.00</jpcrp060_cor:TotalShareholdingRatioOfShareCertificatesEtc>
        </xbrli:xbrl>"""
        zip_data = _make_xbrl_zip(xbrl.encode("utf-8"))
        result = self.client.parse_xbrl_for_holding_data(zip_data)
        assert result["fund_source"] is None
