import numpy as np
import pandas as pd

from app.strategy.base import Context, Position
from app.strategy.examples.bollinger_reversion import BollingerReversion


def _bars(closes: list[float]) -> pd.DataFrame:
    idx = pd.date_range("2026-01-05 09:00", periods=len(closes), freq="5min")
    c = pd.Series(closes, index=idx, dtype=float)
    return pd.DataFrame({"open": c, "high": c, "low": c, "close": c, "volume": 1000.0})


def _closes(extra: list[float] | None = None) -> list[float]:
    rng = np.random.default_rng(5)
    flat = 100 + np.cumsum(rng.normal(0, 0.3, 40))
    dip = flat[-1] + np.cumsum(rng.normal(-1.0, 0.3, 6))
    tail = extra or [dip[-1] + 3.0]
    return list(flat) + list(dip) + tail


def _sig(closes, params=None, position=None):
    strat = BollingerReversion(params or {})
    bars = _bars(closes)
    ctx = Context(
        symbol="X", now=bars.index[-1].to_pydatetime(), bars=bars, position=position or Position()
    )
    return strat.on_bar(ctx)


def test_buy_on_lower_band_reclaim():
    sig = _sig(_closes())
    assert sig is not None and sig.side == "BUY"
    assert "下限反発" in sig.reason


def test_no_buy_while_still_below_band():
    closes = _closes()[:-1]  # 反発前（下限を割ったまま）
    assert _sig(closes) is None


def test_exit_at_mid_band():
    rng = np.random.default_rng(5)
    flat = 100 + np.cumsum(rng.normal(0, 0.3, 40))
    dip = flat[-1] + np.cumsum(rng.normal(-1.0, 0.3, 6))
    tail = [dip[-1] + 3.0, dip[-1] + 6.0]
    closes = list(flat) + list(dip) + tail
    sig = _sig(closes, position=Position(qty=100, avg_price=dip[-1] + 3.0))
    assert sig is not None and sig.side == "EXIT"
    assert "中心線" in sig.reason


def test_exit_at_upper_mode_does_not_fire_at_mid():
    rng = np.random.default_rng(5)
    flat = 100 + np.cumsum(rng.normal(0, 0.3, 40))
    dip = flat[-1] + np.cumsum(rng.normal(-1.0, 0.3, 6))
    tail = [dip[-1] + 3.0, dip[-1] + 6.0]
    closes = list(flat) + list(dip) + tail
    sig = _sig(
        closes, params={"exit_at": "upper"}, position=Position(qty=100, avg_price=dip[-1] + 3.0)
    )
    assert sig is None  # 中心線には届いたが上限にはまだ届いていない


def test_warmup_guard():
    assert _sig([100.0] * 10) is None
