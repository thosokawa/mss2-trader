import numpy as np
import pandas as pd

from app.engine.backtest import run_backtest
from app.strategy.base import Context, Position
from app.strategy.examples.ma_rsi import MaRsi


def _bars(closes: list[float]) -> pd.DataFrame:
    idx = pd.date_range("2026-01-05 09:00", periods=len(closes), freq="5min")
    c = pd.Series(closes, index=idx, dtype=float)
    return pd.DataFrame({"open": c, "high": c + 1, "low": c - 1, "close": c, "volume": 1000.0})


def _regime_change() -> list[float]:
    """下降トレンド → 上昇トレンド（ノイズ付き）。レジーム転換で buy 条件が揃う。"""
    rng = np.random.default_rng(7)
    down = 100 + np.cumsum(rng.normal(-0.3, 1.0, 60))
    up = down[-1] + np.cumsum(rng.normal(0.5, 1.0, 80))
    return list(down) + list(up)


def test_needs_warmup():
    strat = MaRsi()
    bars = _bars([100.0] * 10)
    ctx = Context(symbol="X", now=bars.index[-1].to_pydatetime(), bars=bars, position=Position())
    assert strat.on_bar(ctx) is None


def test_downtrend_no_entry():
    strat = MaRsi({"qty": 100})
    res = run_backtest(strat, _bars(list(np.linspace(200, 100, 80))), "DOWN", warmup=34)
    assert res.metrics.get("trades", 0) == 0


def test_regime_change_produces_round_trips():
    strat = MaRsi(
        {"ma_period": 20, "rsi_min": 40, "rsi_max": 80, "ma_slope_min": -1.0,
         "slope_lookback": 5, "qty": 100}
    )
    res = run_backtest(strat, _bars(_regime_change()), "UP", warmup=34)
    assert res.metrics["trades"] >= 1
    t = res.trades[0]
    assert t.entry_price > 0 and t.exit_ts > t.entry_ts
    assert "EMA20" in t.reason_in and "RSI" in t.reason_in


def test_rsi_band_filter_blocks_entry():
    # RSI 帯を 92-95 に絞ると通常は入らない
    strat = MaRsi({"price_vs_ma": "above", "rsi_min": 92, "rsi_max": 95})
    res = run_backtest(strat, _bars(_regime_change()), "X", warmup=34)
    assert res.metrics.get("trades", 0) == 0


def test_exit_on_ma_break():
    # 建玉ありで終値が MA を大きく下抜け → EXIT
    closes = list(np.linspace(100, 140, 60))  # 上昇で EMA を十分上に
    bars = _bars([*closes, 100.0])            # 最後に急落
    strat = MaRsi({"ma_period": 20})
    ctx = Context(
        symbol="X", now=bars.index[-1].to_pydatetime(), bars=bars,
        position=Position(qty=100, avg_price=130.0),
    )
    sig = strat.on_bar(ctx)
    assert sig is not None and sig.side == "EXIT"
