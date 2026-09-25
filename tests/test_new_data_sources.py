# -*- coding: utf-8 -*-
"""yfinance_data / fred_data 注册表与本地逻辑单测（联网用例标 network）"""
import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ---------------- yfinance ----------------

def test_yf_registry_complete():
    from yfinance_data import YF_FUNCS, TREASURY_TICKERS, MAJOR_INDICES, FX_PAIRS
    assert set(YF_FUNCS) >= {"kline", "info", "treasury", "indices", "fx", "news"}
    for meta in YF_FUNCS.values():
        assert callable(meta["func"])
        assert meta.get("label")
        assert "params" in meta
    assert "^TNX" in TREASURY_TICKERS
    assert "^GSPC" in MAJOR_INDICES
    assert "USDCNY=X" in FX_PAIRS


def test_yf_clean_symbol():
    from yfinance_data import _clean_symbol
    assert _clean_symbol(" aapl ") == "AAPL"
    assert _clean_symbol("0700.hk") == "0700.HK"
    with pytest.raises(ValueError):
        _clean_symbol("  ")


def test_yf_flatten_columns():
    from yfinance_data import _flatten_columns
    cols = pd.MultiIndex.from_tuples(
        [("Close", "AAPL"), ("High", "AAPL"), ("Low", "AAPL")])
    df = pd.DataFrame([[1, 2, 3]], columns=cols)
    out = _flatten_columns(df)
    assert list(out.columns) == ["close_aapl", "high_aapl", "low_aapl"]


@pytest.mark.network
def test_yf_kline_live():
    from yfinance_data import get_kline
    df = get_kline("AAPL", start_date="2024-01-01", end_date="2024-03-01")
    assert df is not None and not df.empty
    assert "close" in [str(c).lower() for c in df.columns] or "Close" in df.columns


@pytest.mark.network
def test_yf_treasury_live():
    from yfinance_data import get_treasury
    df = get_treasury(start_date="2024-06-01", end_date="2024-09-01")
    assert df is not None and not df.empty


# ---------------- FRED ----------------

def test_fred_registry_and_series_catalog():
    from fred_data import FRED_FUNCS, FRED_SERIES
    assert set(FRED_FUNCS) == {"series", "search", "list", "pack"}
    assert "DGS10" in FRED_SERIES
    assert "UNRATE" in FRED_SERIES
    assert len(FRED_SERIES) >= 20
    for sid, meta in FRED_SERIES.items():
        assert meta.get("label") and meta.get("cat") and meta.get("desc")


def test_fred_validate_series_id():
    from fred_data import _validate_series_id
    assert _validate_series_id("dgs10") == "DGS10"
    assert _validate_series_id("BAMLH0A0HYM2") == "BAMLH0A0HYM2"
    with pytest.raises(ValueError):
        _validate_series_id("DROP TABLE--")
    with pytest.raises(ValueError):
        _validate_series_id("")


def test_fred_list_popular_offline():
    from fred_data import list_popular
    df = list_popular()
    assert isinstance(df, pd.DataFrame) and len(df) >= 20
    assert "series_id" in df.columns


def test_fred_search_local():
    from fred_data import search_series
    df = search_series("失业")
    assert not df.empty
    assert (df["series_id"] == "UNRATE").any()
    df2 = search_series("DGS10")
    assert (df2["series_id"] == "DGS10").any()


@pytest.mark.network
def test_fred_series_live():
    from fred_data import get_series
    df = get_series("DGS10", start_date="2024-01-01", end_date="2024-03-01")
    assert not df.empty
    assert set(["series_id", "date", "value"]).issubset(df.columns)
    assert (df["series_id"] == "DGS10").all()


@pytest.mark.network
def test_fred_pack_live():
    from fred_data import get_series_pack
    df = get_series_pack(["DGS10", "DGS2"], start_date="2024-06-01", end_date="2024-09-01")
    assert not df.empty
