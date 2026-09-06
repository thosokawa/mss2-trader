import pandas as pd

from app.engine.backtest import run_backtest
from app.strategy.examples.sma_cross import SmaCross


def _bars(closes: list[float]) -> pd.DataFrame:
    idx = pd.date_range("2026-01-05 09:00", periods=len(closes), freq="5min")
    c = pd.Series(closes, index=idx, dtype=float)
    return pd.DataFrame({"open": c, "high": c, "low": c, "close": c, "volume": 1000.0})


def test_sma_cross_generates_round_trip():
    # 下降 -> 上昇でゴールデンクロス、その後デッドクロスで手仕舞い
    closes = [100 - i for i in range(30)] + [70 + i * 2 for i in range(20)] + [110 - i * 2 for i in range(20)]
    res = run_backtest(SmaCross({"fast": 5, "slow": 20, "qty": 100}), _bars(closes), "TEST", warmup=21)
    assert res.metrics["trades"] >= 1
    t = res.trades[0]
    assert t.qty == 100
    assert t.exit_ts > t.entry_ts


def test_empty_bars_no_crash():
    res = run_backtest(SmaCross(), pd.DataFrame(columns=["open", "high", "low", "close", "volume"]), "X")
    assert "error" in res.metrics


def test_flat_market_no_trades():
    res = run_backtest(SmaCross(), _bars([100.0] * 60), "FLAT", warmup=21)
    assert res.metrics.get("trades", 0) == 0
