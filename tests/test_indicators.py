import numpy as np
import pandas as pd
import pytest

from app.strategy.indicators import (
    adx,
    atr,
    bollinger_bands,
    crossed_down,
    crossed_up,
    deviation_pct,
    donchian_lower,
    donchian_upper,
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


def test_bollinger_bands_ordering():
    s = pd.Series(100 + np.cumsum(np.random.default_rng(1).normal(0, 1, 60)))
    mid, upper, lower = bollinger_bands(s, 20, 2.0)
    assert upper.iloc[-1] > mid.iloc[-1] > lower.iloc[-1]


def test_donchian_excludes_current_bar():
    h = pd.Series([1, 5, 3, 2, 9, 4, 4, 4])
    lo = h - 1
    assert donchian_upper(h, 3).iloc[-1] == 9.0  # 現在足(4)を含まない直前3本の最高値
    assert donchian_lower(lo, 3).iloc[-1] == 3.0


def test_adx_bounds_and_trend_strength():
    n = 100
    rng = np.random.default_rng(2)
    trend = pd.Series(100 + np.arange(n) * 0.8 + rng.normal(0, 0.3, n))
    high, low = trend + 1, trend - 1
    adx_line, plus_di, minus_di = adx(high, low, trend, 14)
    valid = adx_line.dropna()
    assert (valid >= 0).all() and (valid <= 100).all()
    # 強い上昇トレンドでは +DI が -DI を上回る
    assert plus_di.iloc[-1] > minus_di.iloc[-1]
