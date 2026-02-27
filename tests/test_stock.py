"""Tests for stock data endpoint helpers and cache logic."""

import time

import pytest
from unittest.mock import patch

from app.routers.stock import (
    _cache,
    _cache_get,
    _cache_set,
    _format_market_cap,
    _parse_float,
    _parse_int,
)


class TestFormatMarketCap:
    """Tests for _format_market_cap()."""

    def test_cho_and_oku(self):
        # 1兆5000億 = 1,500,000,000,000
        assert _format_market_cap(1_500_000_000_000) == "1兆5000億"

    def test_cho_exact(self):
        assert _format_market_cap(1_000_000_000_000) == "1兆"

    def test_oku(self):
        assert _format_market_cap(500_000_000_000) == "5000億"

    def test_small_oku(self):
        assert _format_market_cap(100_000_000) == "1億"

    def test_less_than_oku(self):
        assert _format_market_cap(50_000_000) == "50000000"

    def test_none_returns_none(self):
        assert _format_market_cap(None) is None

    def test_zero_returns_none(self):
        assert _format_market_cap(0) is None

    def test_negative_returns_none(self):
        assert _format_market_cap(-100) is None


class TestParseHelpers:
    """Tests for _parse_float and _parse_int."""

    def test_parse_float_normal(self):
        assert _parse_float("1234.56") == 1234.56

    def test_parse_float_none(self):
        assert _parse_float(None) is None

    def test_parse_float_na(self):
        assert _parse_float("N/A") is None

    def test_parse_float_dash(self):
        assert _parse_float("-") is None

    def test_parse_float_empty(self):
        assert _parse_float("") is None

    def test_parse_float_whitespace(self):
        assert _parse_float("  123.4  ") == 123.4

    def test_parse_int_normal(self):
        assert _parse_int("42") == 42

    def test_parse_int_float_input(self):
        assert _parse_int("42.7") == 42

    def test_parse_int_none(self):
        assert _parse_int(None) is None


class TestStockCache:
    """Tests for the stock data TTL cache."""

    def setup_method(self):
        """Clear cache before each test."""
        _cache.clear()

    def test_cache_set_and_get(self):
        with patch("app.routers.stock._get_cache_ttl", return_value=1800):
            _cache_set("1234", {"price": 100})
            result = _cache_get("1234")
            assert result == {"price": 100}

    def test_cache_miss_returns_none(self):
        with patch("app.routers.stock._get_cache_ttl", return_value=1800):
            assert _cache_get("9999") is None

    def test_cache_expired_returns_none(self):
        with patch("app.routers.stock._get_cache_ttl", return_value=1):
            _cache_set("1234", {"price": 100})
            # Manually expire by setting timestamp in the past
            _cache["1234"] = (time.monotonic() - 10, {"price": 100})
            assert _cache_get("1234") is None

    def test_cache_eviction_on_max_size(self):
        with patch("app.routers.stock._get_cache_ttl", return_value=1800), \
             patch("app.routers.stock._CACHE_MAX_SIZE", 5):
            for i in range(5):
                _cache_set(str(i), {"i": i})
            assert len(_cache) == 5

            # Setting one more should trigger eviction of expired entries
            # (none are expired, so all stay, plus the new one)
            _cache_set("new", {"i": "new"})
            assert "new" in _cache
