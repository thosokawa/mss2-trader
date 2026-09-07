import numpy as np
import pandas as pd

from app.strategy.base import Context, Position
from app.strategy.examples.trend_rsi_reclaim import TrendRsiReclaim


def _bars(closes: list[float]) -> pd.DataFrame:
    idx = pd.date_range("2026-01-05 09:00", periods=len(closes), freq="5min")
    c = pd.Series(closes, index=idx, dtype=float)
    return pd.DataFrame({"open": c, "high": c + 1, "low": c - 1, "close": c, "volume": 1000.0})


def _sig(closes: list[float], params: dict | None = None):
    strat = TrendRsiReclaim(params or {})
    bars = _bars(closes)
    ctx = Context(symbol="X", now=bars.index[-1].to_pydatetime(), bars=bars, position=Position())
    return strat.on_bar(ctx)


# 強い上昇 → 急な押し目で RSI が 40 割れ → 1本戻して 40 回復（fast>mid 維持）
BUY_CLOSES = (
    list(np.arange(100, 100 + 80 * 1.2, 1.2))
    + list(np.arange(100, 100 + 80 * 1.2, 1.2)[-1] + np.cumsum([-3.5] * 7))
)
BUY_CLOSES = BUY_CLOSES + [BUY_CLOSES[-1] + 5.0]

DOWN = list(np.arange(220, 220 - 80 * 1.2, -1.2))
SELL_CLOSES = DOWN + list(DOWN[-1] + np.cumsum([3.5] * 7))
SELL_CLOSES = SELL_CLOSES + [SELL_CLOSES[-1] - 5.0]


def test_buy_on_rsi_reclaim_in_uptrend():
    sig = _sig(BUY_CLOSES)
    assert sig is not None and sig.side == "BUY"
    assert "40回復" in sig.reason


def test_sell_on_rsi_breakdown_in_downtrend():
    sig = _sig(SELL_CLOSES)
    assert sig is not None and sig.side == "SELL"
    assert "60割れ" in sig.reason


def test_no_signal_without_reclaim():
    # まだ RSI が 40 を回復していない足（押し目の途中）
    partial = BUY_CLOSES[:-1]  # 最後の戻し1本を除く
    assert _sig(partial) is None


def test_trend_filter_blocks_opposite():
    # 上昇の RSI 回復パターンでも、売りレベルを跨がないので SELL は出ない
    sig = _sig(BUY_CLOSES)
    assert sig.side != "SELL"
    # 下降トレンドで RSI が 40 を回復しても BUY は出ない（up_trend が False）
    assert _sig(SELL_CLOSES).side != "BUY"


def test_warmup_guard():
    assert _sig([100.0] * 20) is None
