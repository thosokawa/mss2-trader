from unittest.mock import patch

import pandas as pd
from sqlmodel import Session, select

from app.db import engine, init_db
from app.history import fetch_and_store, fetch_one
from app.models import Bar, Symbol


def _fake_yf_frame(n: int = 5) -> pd.DataFrame:
    idx = pd.date_range("2026-01-01", periods=n, freq="5min", tz="UTC")
    return pd.DataFrame(
        {
            "Open": [100.0] * n, "High": [101.0] * n, "Low": [99.0] * n,
            "Close": [100.5] * n, "Volume": [1000] * n,
        },
        index=idx,
    )


def test_fetch_and_store_success():
    init_db()
    with patch("app.history.yf.download", return_value=_fake_yf_frame()):
        with Session(engine) as s:
            r = fetch_and_store(s, "9001", "5m", "60d")
    assert r["ok"] is True
    assert r["n"] == 5
    with Session(engine) as s:
        assert s.get(Symbol, "9001") is not None
        bars = s.exec(select(Bar).where(Bar.symbol_code == "9001", Bar.timeframe == "5m")).all()
        assert len(bars) == 5


def test_fetch_and_store_empty_result():
    init_db()
    with patch("app.history.yf.download", return_value=pd.DataFrame()):
        with Session(engine) as s:
            r = fetch_and_store(s, "9002", "5m", "60d")
    assert r["ok"] is False
    assert "データ取得できず" in r["message"]


def test_fetch_and_store_exception_is_caught():
    init_db()
    with patch("app.history.yf.download", side_effect=RuntimeError("network down")):
        with Session(engine) as s:
            r = fetch_and_store(s, "9003", "5m", "60d")
    assert r["ok"] is False
    assert "network down" in r["message"]


def test_fetch_one_flattens_multiindex_columns():
    idx = pd.date_range("2026-01-01", periods=3, freq="5min", tz="UTC")
    cols = pd.MultiIndex.from_product([["Open", "High", "Low", "Close", "Volume"], ["9004.T"]])
    df = pd.DataFrame([[100, 101, 99, 100.5, 1000]] * 3, index=idx, columns=cols)
    with patch("app.history.yf.download", return_value=df):
        out = fetch_one("9004", "5m", "60d")
    assert list(out.columns) == ["open", "high", "low", "close", "volume"]
    assert len(out) == 3
