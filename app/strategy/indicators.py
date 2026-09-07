"""テクニカル指標ヘルパー（pandas.Series 入出力・追加依存なし）。

戦略の on_bar から使う。すべて「その足までで計算できる値の列」を返すので、
最新値は `.iloc[-1]`、1本前は `.iloc[-2]`。
"""
from __future__ import annotations

import pandas as pd


def sma(s: pd.Series, n: int) -> pd.Series:
    """単純移動平均。"""
    return s.rolling(int(n)).mean()


def ema(s: pd.Series, n: int) -> pd.Series:
    """指数移動平均。"""
    return s.ewm(span=int(n), adjust=False).mean()


def rsi(s: pd.Series, n: int = 14) -> pd.Series:
    """Wilder の RSI（0-100）。"""
    n = int(n)
    d = s.diff()
    gain = d.clip(lower=0.0)
    loss = -d.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    avg_loss = loss.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    rs = avg_gain / avg_loss
    return 100 - 100 / (1 + rs)


def deviation_pct(price: pd.Series, ma: pd.Series) -> pd.Series:
    """移動平均からの乖離率（%）。price が ma より上なら正。"""
    return (price / ma - 1.0) * 100.0


def slope_pct(s: pd.Series, lookback: int) -> pd.Series:
    """lookback 本前からの変化率（%）。移動平均の傾きの判定に使う。"""
    return (s / s.shift(int(lookback)) - 1.0) * 100.0


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    prev_close = close.shift(1)
    return pd.concat(
        [(high - low), (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)


def atr(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 14) -> pd.Series:
    """Average True Range（Wilder）。"""
    n = int(n)
    return true_range(high, low, close).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def macd(s: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    """(macd 線, signal 線, ヒストグラム) を返す。"""
    line = ema(s, fast) - ema(s, slow)
    sig = line.ewm(span=int(signal), adjust=False).mean()
    return line, sig, line - sig


def crossed_up(a: pd.Series, b: pd.Series) -> bool:
    """最新足で a が b を下から上へ抜けたか（1本前は a<=b、最新は a>b）。"""
    if len(a) < 2 or len(b) < 2:
        return False
    return bool(a.iloc[-2] <= b.iloc[-2] and a.iloc[-1] > b.iloc[-1])


def crossed_down(a: pd.Series, b: pd.Series) -> bool:
    if len(a) < 2 or len(b) < 2:
        return False
    return bool(a.iloc[-2] >= b.iloc[-2] and a.iloc[-1] < b.iloc[-1])
