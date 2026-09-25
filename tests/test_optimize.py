import pandas as pd
import pytest

from app.engine.backtest import run_backtest
from app.engine.optimize import optimize, param_grid
from app.strategy.examples.sma_cross import SmaCross


def _bars(closes: list[float]) -> pd.DataFrame:
    idx = pd.date_range("2026-01-05 09:00", periods=len(closes), freq="5min")
    c = pd.Series(closes, index=idx, dtype=float)
    return pd.DataFrame({"open": c, "high": c, "low": c, "close": c, "volume": 1000.0})


def test_param_grid_basic():
    grid = param_grid({"a": [1, 2], "b": [10, 20], "c": 5})
    assert len(grid) == 4
    assert {"a": 1, "b": 10, "c": 5} in grid
    assert {"a": 2, "b": 20, "c": 5} in grid


def test_param_grid_empty():
    assert param_grid({}) == [{}]


CLOSES = [100 - i for i in range(30)] + [70 + i * 2 for i in range(20)] + [110 - i * 2 for i in range(20)]


def test_split_matches_manual_backtest():
    bars = _bars(CLOSES)
    params = {"fast": 5, "slow": 20, "qty": 100}
    full = run_backtest(SmaCross(params), bars, "X", warmup=21)

    train_ratio = 0.7
    cut_idx = max(1, min(int(len(bars) * train_ratio), len(bars) - 1))
    cut_ts = bars.index[cut_idx]
    expected_train = [t for t in full.trades if t.entry_ts < cut_ts]
    expected_test = [t for t in full.trades if t.entry_ts >= cut_ts]

    rows = optimize(
        SmaCross, bars, "X", grid=params, warmup=21, train_ratio=train_ratio, min_test_trades=0
    )
    assert len(rows) == 1
    assert rows[0].train.get("trades", 0) == len(expected_train)
    assert rows[0].test.get("trades", 0) == len(expected_test)


def test_ranks_trading_combo_above_non_trading():
    bars = _bars(CLOSES)
    # fast==slow(20) だと SMA が常に同値でクロスが起きず 0 取引になる
    rows = optimize(
        SmaCross, bars, "X",
        grid={"fast": [5, 20], "slow": [20], "qty": 100},
        warmup=5, train_ratio=0.6, min_test_trades=1, rank_by="total_pnl",
    )
    assert len(rows) == 2
    assert rows[0].params["fast"] == 5
    assert rows[1].params["fast"] == 20
    assert rows[1].test.get("trades", 0) == 0
    assert "検証期間の取引が少ない" in rows[1].warning


def test_max_combos_guard():
    bars = _bars(CLOSES)
    with pytest.raises(ValueError, match="組み合わせが多すぎます"):
        optimize(SmaCross, bars, "X", grid={"fast": list(range(1, 50))}, max_combos=10)


def test_empty_bars_raises():
    empty = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    with pytest.raises(ValueError, match="足データが空"):
        optimize(SmaCross, empty, "X", grid={})


def test_bad_train_ratio_raises():
    bars = _bars(CLOSES)
    with pytest.raises(ValueError, match="学習期間の割合"):
        optimize(SmaCross, bars, "X", grid={}, train_ratio=0.1)
