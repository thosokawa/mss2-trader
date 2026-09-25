"""パラメータ最適化（グリッドサーチ + ウォークフォワード検証）。

`run_backtest` を1パラメータ組み合わせにつき1回だけ実行し、生成された往復トレードを
時系列で「学習期間」「検証期間」に分けて別々に集計する。ランキングは
**検証期間（探索に使っていない後半のデータ）の成績**で行う — 学習期間だけに
都合よく合わせた過学習の設定を高評価にしないため。

割り切り:
- 現物ロングのみ、成行終値約定（run_backtest と同じ）
- 学習/検証の分割は「バーの本数で前半/後半に分ける」単純な1回きりの split
  （複数回に分けて繰り返すウォークフォワードではない）
- ドローダウンは往復トレードの累積損益から算出する簡易値（バー単位の時価評価ではない）
"""
from __future__ import annotations

from dataclasses import dataclass, field
from itertools import product

import pandas as pd

from app.engine.backtest import Trade, _metrics, run_backtest
from app.strategy.base import Strategy


def param_grid(grid: dict) -> list[dict]:
    """{"fast": [5, 10], "slow": [20, 30], "qty": 100} -> 総当たりの dict のリスト。

    値がリストなら探索対象、リストでなければ固定値として全組み合わせに含める。
    空 dict なら「パラメータ固定なし」で1通り（{}）を返す。
    """
    if not grid:
        return [{}]
    keys = list(grid.keys())
    choices = [v if isinstance(v, list) else [v] for v in grid.values()]
    return [dict(zip(keys, combo, strict=True)) for combo in product(*choices)]


def _metrics_for(trades: list[Trade]) -> dict:
    """トレード列の累積損益を簡易的な資産曲線としてドローダウン込みの指標を出す。"""
    if not trades:
        return {"trades": 0}
    pnls = pd.Series([t.pnl for t in trades])
    equity = pd.Series(pnls.cumsum().to_numpy(), index=[t.exit_ts for t in trades])
    return _metrics(trades, equity)


@dataclass
class OptimizeRow:
    params: dict
    train: dict = field(default_factory=dict)
    test: dict = field(default_factory=dict)
    warning: str = ""


def _rank_value(metrics: dict, rank_by: str) -> float:
    if not metrics.get("trades"):
        return float("-inf")
    v = metrics.get(rank_by)
    if v is None:
        # profit_factor は負けトレード無しだと None（無限大扱い）。他は取引ありなら通常出る。
        v = 999.0 if rank_by == "profit_factor" else 0.0
    return float(v)


def optimize(
    cls: type[Strategy],
    bars: pd.DataFrame,
    symbol: str,
    grid: dict,
    *,
    timeframe: str = "5m",
    warmup: int = 30,
    commission_per_trade: float = 0.0,
    train_ratio: float = 0.7,
    min_test_trades: int = 3,
    rank_by: str = "total_pnl",
    max_combos: int = 200,
) -> list[OptimizeRow]:
    if bars.empty:
        raise ValueError("足データが空です。先に履歴を取得してください。")
    if not 0.3 <= train_ratio <= 0.9:
        raise ValueError("学習期間の割合は 0.3〜0.9 の範囲にしてください。")

    combos = param_grid(grid)
    if len(combos) > max_combos:
        raise ValueError(
            f"組み合わせが多すぎます（{len(combos)} 件 > 上限 {max_combos}）。"
            "パラメータの範囲を絞ってください。"
        )

    cut_idx = max(1, min(int(len(bars) * train_ratio), len(bars) - 1))
    cut_ts = bars.index[cut_idx]

    rows: list[OptimizeRow] = []
    for params in combos:
        strat = cls(dict(params))
        strat.timeframe = timeframe
        res = run_backtest(strat, bars, symbol, warmup=warmup, commission_per_trade=commission_per_trade)
        train_trades = [t for t in res.trades if t.entry_ts < cut_ts]
        test_trades = [t for t in res.trades if t.entry_ts >= cut_ts]
        train_m = _metrics_for(train_trades)
        test_m = _metrics_for(test_trades)
        n_test = test_m.get("trades", 0)
        warning = "" if n_test >= min_test_trades else f"検証期間の取引が少ない(n={n_test})"
        rows.append(OptimizeRow(params=params, train=train_m, test=test_m, warning=warning))

    rows.sort(
        key=lambda r: (
            r.test.get("trades", 0) >= min_test_trades,
            _rank_value(r.test, rank_by),
        ),
        reverse=True,
    )
    return rows
