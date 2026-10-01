import numpy as np
import pandas as pd

from app.strategy.base import Context, Position
from app.strategy.examples.macd_cross import MacdCross


def _bars(closes: list[float]) -> pd.DataFrame:
    idx = pd.date_range("2026-01-05 09:00", periods=len(closes), freq="5min")
    c = pd.Series(closes, index=idx, dtype=float)
    return pd.DataFrame({"open": c, "high": c, "low": c, "close": c, "volume": 1000.0})


def _closes() -> list[float]:
    rng = np.random.default_rng(4)
    down = 100 + np.cumsum(rng.normal(-0.4, 0.6, 50))
    up = down[-1] + np.cumsum(rng.normal(0.5, 0.6, 50))
    peak = up[-1] + np.cumsum(rng.normal(-0.6, 0.5, 20))
    return list(down) + list(up) + list(peak)


def _sig(closes, params=None, position=None):
    strat = MacdCross(params or {})
    bars = _bars(closes)
    ctx = Context(
        symbol="X", now=bars.index[-1].to_pydatetime(), bars=bars, position=position or Position()
    )
    return strat.on_bar(ctx)


ALL_CLOSES = _closes()


def test_buy_on_macd_cross_up():
    # i=97 で MACD がシグナルを上抜け、かつゼロライン上
    sig = _sig(ALL_CLOSES[:98])
    assert sig is not None and sig.side == "BUY"
    assert "MACD上抜け" in sig.reason


def test_require_above_zero_blocks_negative_cross():
    # i=50 の上抜けは MACD がまだマイナス圏
    closes = ALL_CLOSES[:51]
    assert _sig(closes, {"require_above_zero": False}) is not None
    assert _sig(closes, {"require_above_zero": True}) is None


def test_exit_on_macd_cross_down():
    # i=102 で MACD がシグナルを下抜け
    sig = _sig(ALL_CLOSES[:103], position=Position(qty=100, avg_price=ALL_CLOSES[90]))
    assert sig is not None and sig.side == "EXIT"
    assert "MACD下抜け" in sig.reason


def test_warmup_guard():
    assert _sig([100.0] * 10) is None


def test_gap_filter_limits_entries_to_gap_direction():
    """ギャップの向きにだけ建てる（トレンド×RSI出戻りと同じ Strategy.gap_allows）。"""
    import pandas as pd

    from app.strategy.base import Context, Position
    from app.strategy.examples.macd_cross import MacdCross

    n = 80
    closes = [100 - i * 0.3 for i in range(n - 6)] + [100 - (n - 6) * 0.3 + i * 1.5 for i in range(1, 7)]
    i1 = pd.date_range("2026-09-29 00:00", periods=n - 30, freq="5min")
    i2 = pd.date_range("2026-09-30 00:00", periods=30, freq="5min")
    c = pd.Series(closes, index=i1.append(i2), dtype=float)
    bars = pd.DataFrame({"open": c, "high": c + 0.2, "low": c - 0.2, "close": c, "volume": 1000.0})

    def first_buy(params, day2_open):
        b = bars.copy()
        b.iloc[n - 30, b.columns.get_loc("open")] = day2_open
        st = MacdCross(params)
        st.timeframe = "5m"
        for k in range(n - 30, n):
            w = b.iloc[: k + 1]
            sig = st.decide(Context(symbol="X", now=w.index[-1].to_pydatetime(), bars=w, position=Position()))
            if sig is not None and sig.side == "BUY":
                return k
        return None

    prev_close = float(bars["close"].iloc[n - 31])
    assert first_buy({}, prev_close - 5) is not None                       # 既定は絞らない
    assert first_buy({"gap_filter": True}, prev_close + 5) is not None     # ギャップアップの日は買える
    assert first_buy({"gap_filter": True}, prev_close - 5) is None         # ギャップダウンの日は買わない
