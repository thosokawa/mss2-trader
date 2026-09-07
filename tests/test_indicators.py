import numpy as np
import pandas as pd
import pytest

from app.strategy.indicators import (
    atr,
    crossed_down,
    crossed_up,
    deviation_pct,
    ema,
    macd,
    rsi,
    slope_pct,
    sma,
)


def test_sma_ema_basic():
    s = pd.Series([1.0, 2, 3, 4, 5])
    assert sma(s, 2).iloc[-1] == 4.5
    assert ema(s, 2).iloc[-1] > sma(s, 2).iloc[-1]  # 直近重視で上向き列なら上


def test_rsi_bounds_and_extremes():
    up = pd.Series(np.arange(1, 60, dtype=float))
    down = pd.Series(np.arange(60, 1, -1, dtype=float))
    r_up = rsi(up, 14).iloc[-1]
    r_down = rsi(down, 14).iloc[-1]
    assert r_up == 100.0
    assert r_down == 0.0
    mixed = pd.Series(np.sin(np.linspace(0, 12, 100)) * 5 + 100)
    r = rsi(mixed, 14).dropna()
    assert (r >= 0).all() and (r <= 100).all()


def test_atr_positive():
    n = 50
    close = pd.Series(np.linspace(100, 120, n))
    high = close + 1.5
    low = close - 1.5
    a = atr(high, low, close, 14).iloc[-1]
    assert a > 0


def test_deviation_and_slope():
    ma = pd.Series([100.0] * 10)
    price = pd.Series([110.0] * 10)
    assert deviation_pct(price, ma).iloc[-1] == pytest.approx(10.0)
    rising = pd.Series([100.0, 101, 102, 103, 104, 105])
    assert slope_pct(rising, 5).iloc[-1] == pytest.approx(5.0)


def test_cross_helpers():
    a = pd.Series([1.0, 2.0])
    b = pd.Series([1.5, 1.5])
    assert crossed_up(a, b)
    assert not crossed_down(a, b)
    assert crossed_down(pd.Series([2.0, 1.0]), b)


def test_macd_shapes():
    s = pd.Series(np.random.default_rng(0).normal(100, 2, 100).cumsum() / 10 + 100)
    line, sig, hist = macd(s)
    assert len(line) == len(s) == len(sig) == len(hist)
