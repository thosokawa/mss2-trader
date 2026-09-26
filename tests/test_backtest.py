import pandas as pd
import pytest

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


def _stop_test_closes() -> list[float]:
    # GC が index 36（終値82）で発生 -> 直後に急落。自然な DC は3本後(70.0)まで起きない。
    decline = [100 - i for i in range(30)]
    rise = [70 + i * 2 for i in range(7)]
    return decline + rise + [70.0, 70.0, 70.0]


def test_stop_loss_exits_before_natural_signal():
    closes = _stop_test_closes()
    res = run_backtest(
        SmaCross({"fast": 5, "slow": 20, "qty": 100, "stop_loss_pct": 3.0}), _bars(closes), "X", warmup=21
    )
    assert len(res.trades) == 1
    t = res.trades[0]
    assert t.entry_price == 82.0
    assert t.exit_price == pytest.approx(82.0 * 0.97)
    assert "損切り" in t.reason_out
    # 損切り無しなら自然な DC まで持ち越して、もっと不利な価格で仕切っていたはず
    plain = run_backtest(SmaCross({"fast": 5, "slow": 20, "qty": 100}), _bars(closes), "X", warmup=21)
    assert plain.trades[0].exit_ts > t.exit_ts
    assert plain.trades[0].exit_price < t.exit_price


def test_take_profit_exits_before_natural_signal():
    # 上と対称: 急騰バーで利確ラインに届く
    decline = [100 - i for i in range(30)]
    rise = [70 + i * 2 for i in range(7)]  # GC index36, close 82
    closes = decline + rise + [95.0, 95.0, 95.0]
    res = run_backtest(
        SmaCross({"fast": 5, "slow": 20, "qty": 100, "take_profit_pct": 5.0}), _bars(closes), "X", warmup=21
    )
    assert len(res.trades) == 1
    t = res.trades[0]
    assert t.exit_price == pytest.approx(82.0 * 1.05)
    assert "利確" in t.reason_out


def test_stop_and_target_together_prefers_stop_same_bar():
    # GC(index36, close82) の直後の足で、高値は利確ラインを、安値は損切りラインを
    # 両方満たすように high/low だけ広げる（close はそのまま）
    closes = _stop_test_closes()
    bars = _bars(closes)
    exit_idx = 37  # entry の1本後
    bars.loc[bars.index[exit_idx], "high"] = 82.0 * 1.10  # 利確(5%)を大きく超える
    bars.loc[bars.index[exit_idx], "low"] = 82.0 * 0.90   # 損切り(3%)を大きく超える

    res = run_backtest(
        SmaCross({"fast": 5, "slow": 20, "qty": 100, "stop_loss_pct": 3.0, "take_profit_pct": 5.0}),
        bars, "X", warmup=21,
    )
    assert len(res.trades) == 1
    assert "損切り" in res.trades[0].reason_out
    assert res.trades[0].exit_price == pytest.approx(82.0 * 0.97)
