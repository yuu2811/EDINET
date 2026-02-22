"""Tests for shared dependencies and validators."""

import pytest
from fastapi import HTTPException

from app.deps import (
    normalize_sec_code,
    validate_doc_id,
    validate_edinet_code,
    validate_sec_code,
)


class TestNormalizeSecCode:
    def test_four_digit(self):
        assert normalize_sec_code("7203") == "7203"

    def test_five_digit_strips_check(self):
        assert normalize_sec_code("72030") == "7203"

    def test_none(self):
        assert normalize_sec_code(None) is None

    def test_empty(self):
        assert normalize_sec_code("") is None

    def test_whitespace(self):
        assert normalize_sec_code("  7203  ") == "7203"

    def test_non_numeric(self):
        assert normalize_sec_code("ABCD") is None

    def test_too_short(self):
        assert normalize_sec_code("123") is None

    def test_too_long(self):
        assert normalize_sec_code("123456") is None


class TestValidateSecCode:
    def test_valid(self):
        assert validate_sec_code("7203") == "7203"

    def test_invalid_raises(self):
        with pytest.raises(HTTPException) as exc_info:
            validate_sec_code("abc")
        assert exc_info.value.status_code == 400


class TestValidateEdinetCode:
    def test_valid(self):
        assert validate_edinet_code("E12345") == "E12345"

    def test_valid_with_whitespace(self):
        assert validate_edinet_code("  E12345  ") == "E12345"

    def test_invalid_no_prefix(self):
        with pytest.raises(HTTPException) as exc_info:
            validate_edinet_code("12345")
        assert exc_info.value.status_code == 400

    def test_invalid_too_short(self):
        with pytest.raises(HTTPException) as exc_info:
            validate_edinet_code("E123")
        assert exc_info.value.status_code == 400

    def test_invalid_too_long(self):
        with pytest.raises(HTTPException) as exc_info:
            validate_edinet_code("E123456")
        assert exc_info.value.status_code == 400

    def test_invalid_non_numeric(self):
        with pytest.raises(HTTPException) as exc_info:
            validate_edinet_code("EABCDE")
        assert exc_info.value.status_code == 400

    def test_invalid_empty(self):
        with pytest.raises(HTTPException) as exc_info:
            validate_edinet_code("")
        assert exc_info.value.status_code == 400


class TestValidateDocId:
    def test_valid(self):
        assert validate_doc_id("S100ABC1") == "S100ABC1"

    def test_valid_long(self):
        assert validate_doc_id("S100ABCDEF12") == "S100ABCDEF12"

    def test_valid_with_whitespace(self):
        assert validate_doc_id("  S100ABC1  ") == "S100ABC1"

    def test_invalid_no_prefix(self):
        with pytest.raises(HTTPException) as exc_info:
            validate_doc_id("100ABC1")
        assert exc_info.value.status_code == 400

    def test_invalid_too_short(self):
        with pytest.raises(HTTPException) as exc_info:
            validate_doc_id("S12345")
        assert exc_info.value.status_code == 400

    def test_invalid_special_chars(self):
        with pytest.raises(HTTPException) as exc_info:
            validate_doc_id("S100-ABC!")
        assert exc_info.value.status_code == 400

    def test_invalid_empty(self):
        with pytest.raises(HTTPException) as exc_info:
            validate_doc_id("")
        assert exc_info.value.status_code == 400

    def test_invalid_path_traversal(self):
        with pytest.raises(HTTPException) as exc_info:
            validate_doc_id("../../../etc/passwd")
        assert exc_info.value.status_code == 400
