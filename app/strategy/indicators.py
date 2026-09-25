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


def bollinger_bands(s: pd.Series, n: int = 20, num_std: float = 2.0):
    """(中心線, 上限, 下限) を返す。中心線=SMA、上下限=中心線±num_std×標準偏差。"""
    mid = sma(s, n)
    std = s.rolling(int(n)).std()
    upper = mid + num_std * std
    lower = mid - num_std * std
    return mid, upper, lower


def donchian_upper(high: pd.Series, n: int) -> pd.Series:
    """直近 n 本（現在足を含まない）の最高値。ブレイクアウト判定に使う。"""
    return high.rolling(int(n)).max().shift(1)


def donchian_lower(low: pd.Series, n: int) -> pd.Series:
    """直近 n 本（現在足を含まない）の最安値。"""
    return low.rolling(int(n)).min().shift(1)


def adx(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 14):
    """Wilder の ADX。(ADX, +DI, -DI) を返す。ADX が高いほどトレンドが強い（方向は問わない）。"""
    n = int(n)
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
    minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)
    tr_n = true_range(high, low, close).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    plus_di = 100 * plus_dm.ewm(alpha=1 / n, adjust=False, min_periods=n).mean() / tr_n
    minus_di = 100 * minus_dm.ewm(alpha=1 / n, adjust=False, min_periods=n).mean() / tr_n
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di)
    adx_line = dx.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    return adx_line, plus_di, minus_di


def crossed_up(a: pd.Series, b: pd.Series) -> bool:
    """最新足で a が b を下から上へ抜けたか（1本前は a<=b、最新は a>b）。"""
    if len(a) < 2 or len(b) < 2:
        return False
    return bool(a.iloc[-2] <= b.iloc[-2] and a.iloc[-1] > b.iloc[-1])


def crossed_down(a: pd.Series, b: pd.Series) -> bool:
    if len(a) < 2 or len(b) < 2:
        return False
    return bool(a.iloc[-2] >= b.iloc[-2] and a.iloc[-1] < b.iloc[-1])
