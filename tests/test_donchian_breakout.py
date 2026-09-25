import numpy as np
import pandas as pd

from app.strategy.base import Context, Position
from app.strategy.examples.donchian_breakout import DonchianBreakout


def _bars(closes: list[float]) -> pd.DataFrame:
    idx = pd.date_range("2026-01-05 09:00", periods=len(closes), freq="5min")
    c = pd.Series(closes, index=idx, dtype=float)
    return pd.DataFrame({"open": c, "high": c + 0.5, "low": c - 0.5, "close": c, "volume": 1000.0})


def _range_closes() -> list[float]:
    rng = np.random.default_rng(6)
    return list(100 + rng.normal(0, 1, 25).cumsum() * 0.1)


def _sig(closes, params=None, position=None):
    strat = DonchianBreakout(params or {})
    bars = _bars(closes)
    ctx = Context(
        symbol="X", now=bars.index[-1].to_pydatetime(), bars=bars, position=position or Position()
    )
    return strat.on_bar(ctx)


def test_buy_on_breakout():
    base = _range_closes()
    breakout = [max(base) + 3.0]
    sig = _sig(base + breakout, {"entry_period": 20, "exit_period": 10})
    assert sig is not None and sig.side == "BUY"
    assert "高値ブレイク" in sig.reason


def test_no_buy_inside_range():
    base = _range_closes()
    assert _sig(base, {"entry_period": 20, "exit_period": 10}) is None


def test_exit_on_low_break():
    base = _range_closes()
    peak = max(base) + 3.0
    closes = base + [peak, peak + 2, peak + 1, peak - 5]
    sig = _sig(
        closes, {"entry_period": 20, "exit_period": 10}, position=Position(qty=100, avg_price=peak)
    )
    assert sig is not None and sig.side == "EXIT"
    assert "安値割れ" in sig.reason


def test_warmup_guard():
    assert _sig([100.0] * 10) is None
