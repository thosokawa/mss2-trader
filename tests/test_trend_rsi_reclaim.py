import numpy as np
import pandas as pd

from app.strategy.base import Context, Position
from app.strategy.examples.trend_rsi_reclaim import TrendRsiReclaim


def _bars(closes: list[float]) -> pd.DataFrame:
    idx = pd.date_range("2026-01-05 09:00", periods=len(closes), freq="5min")
    c = pd.Series(closes, index=idx, dtype=float)
    return pd.DataFrame({"open": c, "high": c + 1, "low": c - 1, "close": c, "volume": 1000.0})


def _sig(closes: list[float], params: dict | None = None, position: Position | None = None):
    strat = TrendRsiReclaim(params or {})
    bars = _bars(closes)
    ctx = Context(symbol="X", now=bars.index[-1].to_pydatetime(), bars=bars, position=position or Position())
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


# 上昇のあと、ゆるやかに下げて短期MA<中期MAになった足（RSI はまだ 60 を割った瞬間ではない）
FLIP_DOWN = list(np.arange(100, 100 + 60 * 1.2, 1.2)) + list(100 + 59 * 1.2 + np.cumsum([-0.8] * 15))
FLIP_UP = list(np.arange(220, 220 - 60 * 1.2, -1.2)) + list(220 - 59 * 1.2 + np.cumsum([0.8] * 15))


def test_trend_flip_exit_is_off_by_default():
    assert _sig(FLIP_DOWN, position=Position(qty=100, avg_price=150)) is None
    assert _sig(FLIP_UP, position=Position(qty=-100, avg_price=150), params={"direction": "both"}) is None


def test_trend_flip_exit_closes_long_and_short():
    on = {"exit_on_trend_flip": True}
    sig = _sig(FLIP_DOWN, params=on, position=Position(qty=100, avg_price=150))
    assert sig is not None and sig.side == "EXIT" and "トレンド反転" in sig.reason
    sig = _sig(FLIP_UP, params={**on, "direction": "both"}, position=Position(qty=-100, avg_price=150))
    assert sig is not None and sig.side == "EXIT"
    # 建玉が無ければ何もしない（新規建てはこれまでどおり RSI の条件で）
    assert _sig(FLIP_DOWN, params=on) is None
    # 建玉とトレンドが同じ向きなら手仕舞わない
    assert _sig(FLIP_UP, params=on, position=Position(qty=100, avg_price=150)) is None
