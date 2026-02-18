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
