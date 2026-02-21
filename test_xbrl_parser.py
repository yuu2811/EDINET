"""Test XBRL parser with sample data matching EDINET structures."""
import io
import zipfile
from app.edinet import EdinetClient


def _make_zip(files: dict[str, bytes]) -> bytes:
    """Create an in-memory ZIP with given filename→content mapping."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


# --- Test 1: Traditional XBRL in PublicDoc/ ---
TRADITIONAL_XBRL = """<?xml version="1.0" encoding="UTF-8"?>
<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance"
            xmlns:jpcrp_cor="http://disclosure.edinet-fsa.go.jp/taxonomy/jpcrp/2024-01-01/jpcrp_cor">
  <jpcrp_cor:TotalShareholdingRatioOfShareCertificatesEtc contextRef="FilingDateInstant" unitRef="pure" decimals="2">5.23</jpcrp_cor:TotalShareholdingRatioOfShareCertificatesEtc>
  <jpcrp_cor:TotalShareholdingRatioOfShareCertificatesEtc contextRef="PriorFilingDateInstant" unitRef="pure" decimals="2">4.10</jpcrp_cor:TotalShareholdingRatioOfShareCertificatesEtc>
  <jpcrp_cor:NameOfLargeShareholdingReporter contextRef="FilingDateInstant">テスト株式会社</jpcrp_cor:NameOfLargeShareholdingReporter>
  <jpcrp_cor:IssuerNameLargeShareholding contextRef="FilingDateInstant">ターゲット株式会社</jpcrp_cor:IssuerNameLargeShareholding>
  <jpcrp_cor:SecurityCodeOfIssuer contextRef="FilingDateInstant">12340</jpcrp_cor:SecurityCodeOfIssuer>
  <jpcrp_cor:TotalNumberOfShareCertificatesEtcHeld contextRef="FilingDateInstant" unitRef="shares" decimals="0">1000000</jpcrp_cor:TotalNumberOfShareCertificatesEtcHeld>
  <jpcrp_cor:PurposeOfHoldingOfShareCertificatesEtc contextRef="FilingDateInstant">純投資</jpcrp_cor:PurposeOfHoldingOfShareCertificatesEtc>
</xbrli:xbrl>
""".encode("utf-8")

# --- Test 2: Inline XBRL (.htm) in PublicDoc/ ---
INLINE_XBRL_HTM = """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"
      xmlns:ix="http://www.xbrl.org/2013/inlineXBRL"
      xmlns:jpcrp_cor="http://disclosure.edinet-fsa.go.jp/taxonomy/jpcrp/2024-01-01/jpcrp_cor"
      xmlns:xbrli="http://www.xbrl.org/2003/instance">
<head><title>Large Shareholding Report</title></head>
<body>
<div>
  <ix:header>
    <ix:references>
      <link:schemaRef xmlns:link="http://www.xbrl.org/2003/linkbase" xlink:type="simple" xlink:href="jpcrp060300-q1r-001_E00001-000.xsd" xmlns:xlink="http://www.w3.org/1999/xlink"/>
    </ix:references>
    <ix:resources>
      <xbrli:context id="FilingDateInstant"><xbrli:entity><xbrli:identifier scheme="http://disclosure.edinet-fsa.go.jp">E00001</xbrli:identifier></xbrli:entity><xbrli:period><xbrli:instant>2024-06-15</xbrli:instant></xbrli:period></xbrli:context>
      <xbrli:context id="PriorFilingDateInstant"><xbrli:entity><xbrli:identifier scheme="http://disclosure.edinet-fsa.go.jp">E00001</xbrli:identifier></xbrli:entity><xbrli:period><xbrli:instant>2024-03-15</xbrli:instant></xbrli:period></xbrli:context>
      <xbrli:unit id="pure"><xbrli:measure>xbrli:pure</xbrli:measure></xbrli:unit>
      <xbrli:unit id="shares"><xbrli:measure>xbrli:shares</xbrli:measure></xbrli:unit>
    </ix:resources>
  </ix:header>
  <p>Filer: <ix:nonNumeric name="jpcrp_cor:NameOfLargeShareholdingReporter" contextRef="FilingDateInstant">テスト投資顧問</ix:nonNumeric></p>
  <p>Issuer: <ix:nonNumeric name="jpcrp_cor:IssuerNameLargeShareholding" contextRef="FilingDateInstant">対象企業株式会社</ix:nonNumeric></p>
  <p>Code: <ix:nonNumeric name="jpcrp_cor:SecurityCodeOfIssuer" contextRef="FilingDateInstant">56780</ix:nonNumeric></p>
  <p>Current ratio: <ix:nonFraction name="jpcrp_cor:TotalShareholdingRatioOfShareCertificatesEtc" contextRef="FilingDateInstant" unitRef="pure" decimals="2">7.45</ix:nonFraction>%%</p>
  <p>Previous ratio: <ix:nonFraction name="jpcrp_cor:TotalShareholdingRatioOfShareCertificatesEtc" contextRef="PriorFilingDateInstant" unitRef="pure" decimals="2">6.20</ix:nonFraction>%%</p>
  <p>Shares: <ix:nonFraction name="jpcrp_cor:TotalNumberOfShareCertificatesEtcHeld" contextRef="FilingDateInstant" unitRef="shares" decimals="0">2500000</ix:nonFraction></p>
  <p>Purpose: <ix:nonNumeric name="jpcrp_cor:PurposeOfHoldingOfShareCertificatesEtc" contextRef="FilingDateInstant">純投資</ix:nonNumeric></p>
</div>
</body>
</html>
""".encode("utf-8")

# --- Test 3: Traditional XBRL NOT in PublicDoc (flat structure) ---
FLAT_XBRL = TRADITIONAL_XBRL  # Same content, different path


# --- Test 4: Inline XBRL with non-standard namespace prefix ---
INLINE_XBRL_ALT_NS = """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"
      xmlns:ixt="http://www.xbrl.org/2013/inlineXBRL"
      xmlns:jpcrp_cor="http://disclosure.edinet-fsa.go.jp/taxonomy/jpcrp/2024-01-01/jpcrp_cor"
      xmlns:xbrli="http://www.xbrl.org/2003/instance">
<head><title>Test</title></head>
<body>
  <p>Ratio: <ixt:nonFraction name="jpcrp_cor:TotalShareholdingRatioOfShareCertificatesEtc" contextRef="FilingDateInstant" unitRef="pure" decimals="2">3.50</ixt:nonFraction>%%</p>
</body>
</html>
""".encode("utf-8")


# --- Test 6: Traditional XBRL with jplvh_cor namespace (real EDINET 大量保有報告書) ---
JPLVH_TRADITIONAL_XBRL = """<?xml version="1.0" encoding="UTF-8"?>
<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance"
            xmlns:jplvh_cor="http://disclosure.edinet-fsa.go.jp/taxonomy/jplvh/2023-11-01/jplvh_cor">
  <jplvh_cor:HoldingRatioOfShareCertificatesEtc contextRef="FilingDateInstant" unitRef="pure" decimals="4">9.67</jplvh_cor:HoldingRatioOfShareCertificatesEtc>
  <jplvh_cor:HoldingRatioOfShareCertificatesEtc contextRef="PriorFilingDateInstant" unitRef="pure" decimals="4">7.23</jplvh_cor:HoldingRatioOfShareCertificatesEtc>
  <jplvh_cor:Name contextRef="FilingDateInstant">ゴールドマン・サックス証券株式会社</jplvh_cor:Name>
  <jplvh_cor:NameOfIssuer contextRef="FilingDateInstant">ソニーグループ株式会社</jplvh_cor:NameOfIssuer>
  <jplvh_cor:SecurityCodeOfIssuer contextRef="FilingDateInstant">67580</jplvh_cor:SecurityCodeOfIssuer>
  <jplvh_cor:TotalNumberOfStocksEtcHeld contextRef="FilingDateInstant" unitRef="shares" decimals="0">5800000</jplvh_cor:TotalNumberOfStocksEtcHeld>
  <jplvh_cor:PurposeOfHolding contextRef="FilingDateInstant">純投資</jplvh_cor:PurposeOfHolding>
</xbrli:xbrl>
""".encode("utf-8")

# --- Test 7: Inline XBRL with jplvh_cor namespace ---
JPLVH_INLINE_XBRL = """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"
      xmlns:ix="http://www.xbrl.org/2013/inlineXBRL"
      xmlns:jplvh_cor="http://disclosure.edinet-fsa.go.jp/taxonomy/jplvh/2023-11-01/jplvh_cor"
      xmlns:xbrli="http://www.xbrl.org/2003/instance">
<head><title>Large Shareholding Report</title></head>
<body>
<div>
  <ix:header>
    <ix:references>
      <link:schemaRef xmlns:link="http://www.xbrl.org/2003/linkbase" xlink:type="simple" xlink:href="jplvh060300-q1r-001_E00001-000.xsd" xmlns:xlink="http://www.w3.org/1999/xlink"/>
    </ix:references>
    <ix:resources>
      <xbrli:context id="FilingDateInstant"><xbrli:entity><xbrli:identifier scheme="http://disclosure.edinet-fsa.go.jp">E00001</xbrli:identifier></xbrli:entity><xbrli:period><xbrli:instant>2024-06-15</xbrli:instant></xbrli:period></xbrli:context>
      <xbrli:context id="PriorFilingDateInstant"><xbrli:entity><xbrli:identifier scheme="http://disclosure.edinet-fsa.go.jp">E00001</xbrli:identifier></xbrli:entity><xbrli:period><xbrli:instant>2024-03-15</xbrli:instant></xbrli:period></xbrli:context>
      <xbrli:unit id="pure"><xbrli:measure>xbrli:pure</xbrli:measure></xbrli:unit>
      <xbrli:unit id="shares"><xbrli:measure>xbrli:shares</xbrli:measure></xbrli:unit>
    </ix:resources>
  </ix:header>
  <p>Filer: <ix:nonNumeric name="jplvh_cor:Name" contextRef="FilingDateInstant">ブラックロック・ジャパン株式会社</ix:nonNumeric></p>
  <p>Issuer: <ix:nonNumeric name="jplvh_cor:NameOfIssuer" contextRef="FilingDateInstant">トヨタ自動車株式会社</ix:nonNumeric></p>
  <p>Code: <ix:nonNumeric name="jplvh_cor:SecurityCodeOfIssuer" contextRef="FilingDateInstant">72030</ix:nonNumeric></p>
  <p>Current ratio: <ix:nonFraction name="jplvh_cor:HoldingRatioOfShareCertificatesEtc" contextRef="FilingDateInstant" unitRef="pure" decimals="2">5.12</ix:nonFraction>%%</p>
  <p>Previous ratio: <ix:nonFraction name="jplvh_cor:HoldingRatioOfShareCertificatesEtc" contextRef="PriorFilingDateInstant" unitRef="pure" decimals="2">4.85</ix:nonFraction>%%</p>
  <p>Shares: <ix:nonFraction name="jplvh_cor:TotalNumberOfStocksEtcHeld" contextRef="FilingDateInstant" unitRef="shares" decimals="0">8300000</ix:nonFraction></p>
  <p>Purpose: <ix:nonNumeric name="jplvh_cor:PurposeOfHolding" contextRef="FilingDateInstant">純投資</ix:nonNumeric></p>
</div>
</body>
</html>
""".encode("utf-8")


def run_tests():
    client = EdinetClient()
    passed = 0
    failed = 0

    def check(name, result, field, expected):
        nonlocal passed, failed
        actual = result.get(field)
        if actual == expected:
            print(f"  PASS {field}: {actual}")
            passed += 1
        else:
            print(f"  FAIL {field}: expected {expected}, got {actual}")
            failed += 1

    # Test 1: Traditional XBRL in PublicDoc/
    print("\n=== Test 1: Traditional XBRL in PublicDoc/ ===")
    z1 = _make_zip({"XBRL/PublicDoc/report.xbrl": TRADITIONAL_XBRL})
    r1 = client.parse_xbrl_for_holding_data(z1)
    print(f"  Result: {r1}")
    check("T1", r1, "holding_ratio", 5.23)
    check("T1", r1, "previous_holding_ratio", 4.10)
    check("T1", r1, "holder_name", "テスト株式会社")
    check("T1", r1, "target_company_name", "ターゲット株式会社")
    check("T1", r1, "target_sec_code", "12340")
    check("T1", r1, "shares_held", 1000000)

    # Test 2: Inline XBRL in PublicDoc/
    print("\n=== Test 2: Inline XBRL (.htm) in PublicDoc/ ===")
    z2 = _make_zip({"XBRL/PublicDoc/report.htm": INLINE_XBRL_HTM})
    r2 = client.parse_xbrl_for_holding_data(z2)
    print(f"  Result: {r2}")
    check("T2", r2, "holding_ratio", 7.45)
    check("T2", r2, "previous_holding_ratio", 6.20)
    check("T2", r2, "holder_name", "テスト投資顧問")
    check("T2", r2, "target_company_name", "対象企業株式会社")
    check("T2", r2, "target_sec_code", "56780")
    check("T2", r2, "shares_held", 2500000)

    # Test 3: Flat structure (no PublicDoc/)
    print("\n=== Test 3: Traditional XBRL (flat, no PublicDoc/) ===")
    z3 = _make_zip({"jpcrp060300-q1r-001_E00001-000.xbrl": FLAT_XBRL})
    r3 = client.parse_xbrl_for_holding_data(z3)
    print(f"  Result: {r3}")
    check("T3", r3, "holding_ratio", 5.23)

    # Test 4: Flat .htm structure (no PublicDoc/)
    print("\n=== Test 4: Inline XBRL (.htm, flat, no PublicDoc/) ===")
    z4 = _make_zip({"jpcrp060300-q1r-001_E00001-000.htm": INLINE_XBRL_HTM})
    r4 = client.parse_xbrl_for_holding_data(z4)
    print(f"  Result: {r4}")
    check("T4", r4, "holding_ratio", 7.45)

    # Test 5: Alternative namespace prefix
    print("\n=== Test 5: Inline XBRL with non-standard ix namespace ===")
    z5 = _make_zip({"XBRL/PublicDoc/report.htm": INLINE_XBRL_ALT_NS})
    r5 = client.parse_xbrl_for_holding_data(z5)
    print(f"  Result: {r5}")
    check("T5", r5, "holding_ratio", 3.50)

    # Test 6: Traditional XBRL with jplvh_cor namespace (real EDINET taxonomy)
    print("\n=== Test 6: Traditional XBRL with jplvh_cor (大量保有) ===")
    z6 = _make_zip({"XBRL/PublicDoc/report.xbrl": JPLVH_TRADITIONAL_XBRL})
    r6 = client.parse_xbrl_for_holding_data(z6)
    print(f"  Result: {r6}")
    check("T6", r6, "holding_ratio", 9.67)
    check("T6", r6, "previous_holding_ratio", 7.23)
    check("T6", r6, "holder_name", "ゴールドマン・サックス証券株式会社")
    check("T6", r6, "target_company_name", "ソニーグループ株式会社")
    check("T6", r6, "target_sec_code", "67580")
    check("T6", r6, "shares_held", 5800000)
    check("T6", r6, "purpose_of_holding", "純投資")

    # Test 7: Inline XBRL with jplvh_cor namespace
    print("\n=== Test 7: Inline XBRL with jplvh_cor (大量保有) ===")
    z7 = _make_zip({"XBRL/PublicDoc/report.htm": JPLVH_INLINE_XBRL})
    r7 = client.parse_xbrl_for_holding_data(z7)
    print(f"  Result: {r7}")
    check("T7", r7, "holding_ratio", 5.12)
    check("T7", r7, "previous_holding_ratio", 4.85)
    check("T7", r7, "holder_name", "ブラックロック・ジャパン株式会社")
    check("T7", r7, "target_company_name", "トヨタ自動車株式会社")
    check("T7", r7, "target_sec_code", "72030")
    check("T7", r7, "shares_held", 8300000)
    check("T7", r7, "purpose_of_holding", "純投資")

    print(f"\n{'='*50}")
    print(f"Results: {passed} passed, {failed} failed")
    return failed == 0


if __name__ == "__main__":
    import sys
    sys.exit(0 if run_tests() else 1)
