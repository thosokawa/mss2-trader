import numpy as np
import pandas as pd

from app.strategy.base import Context, Position
from app.strategy.examples.adx_ma_cross import AdxMaCross


def _bars(closes: list[float]) -> pd.DataFrame:
    idx = pd.date_range("2026-01-05 09:00", periods=len(closes), freq="5min")
    c = pd.Series(closes, index=idx, dtype=float)
    return pd.DataFrame({"open": c, "high": c + 0.5, "low": c - 0.5, "close": c, "volume": 1000.0})


def _sig(closes, params=None, position=None):
    strat = AdxMaCross(params or {})
    bars = _bars(closes)
    ctx = Context(
        symbol="X", now=bars.index[-1].to_pydatetime(), bars=bars, position=position or Position()
    )
    return strat.on_bar(ctx)


def _trend_closes(n_down=30, n_up=30, n_down2=0):
    rng = np.random.default_rng(8)
    down = 100 + np.cumsum(rng.normal(-0.8, 0.3, n_down))
    up = down[-1] + np.cumsum(rng.normal(0.8, 0.3, n_up))
    out = list(down) + list(up)
    if n_down2:
        down2 = up[-1] + np.cumsum(rng.normal(-0.8, 0.3, n_down2))
        out += list(down2)
    return out


def _choppy_closes(n=80):
    rng = np.random.default_rng(9)
    return list(100 + np.sin(np.linspace(0, 15, n)) * 2 + rng.normal(0, 0.3, n))


def test_buy_on_gc_with_strong_trend():
    closes = _trend_closes()[:38]  # GC は i=37、ADX≈73（強）
    sig = _sig(closes)
    assert sig is not None and sig.side == "BUY"
    assert "強" in sig.reason


def test_choppy_gc_blocked_by_adx_filter():
    closes = _choppy_closes()[:35]  # GC は i=34、ADX≈19.8（閾値20未満）
    assert _sig(closes) is None


def test_lower_adx_min_allows_choppy_entry():
    closes = _choppy_closes()[:35]
    sig = _sig(closes, {"adx_min": 10.0})
    assert sig is not None and sig.side == "BUY"


def test_exit_on_dead_cross():
    closes = _trend_closes(n_down2=15)[:69]  # DC は i=68
    sig = _sig(closes, position=Position(qty=100, avg_price=closes[40]))
    assert sig is not None and sig.side == "EXIT"


def test_warmup_guard():
    assert _sig([100.0] * 10) is None
