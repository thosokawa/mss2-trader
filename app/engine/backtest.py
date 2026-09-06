"""過去足に対して Strategy.on_bar() を1本ずつ流し、擬似的に売買してパフォーマンスを測る。

P0 の割り切り:
- 現物ロング only（空売り・信用の建玉管理は P3 以降）
- 約定は「シグナルが出た足の終値」で成立（スリッページ/板は考慮しない）
- 手数料は commission_per_trade（片道・円）で概算
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import pandas as pd

from app.strategy.base import Context, Position, Strategy


@dataclass
class Trade:
    entry_ts: datetime
    entry_price: float
    exit_ts: datetime
    exit_price: float
    qty: int
    pnl: float
    return_pct: float
    reason_in: str = ""
    reason_out: str = ""


@dataclass
class BacktestResult:
    symbol: str
    timeframe: str
    trades: list[Trade] = field(default_factory=list)
    equity_curve: pd.Series | None = None
    metrics: dict = field(default_factory=dict)


def run_backtest(
    strategy: Strategy,
    bars: pd.DataFrame,
    symbol: str,
    *,
    warmup: int = 30,
    commission_per_trade: float = 0.0,
) -> BacktestResult:
    if bars.empty:
        return BacktestResult(symbol, strategy.timeframe, metrics={"error": "足データが空"})

    position = Position()
    trades: list[Trade] = []
    pending_entry: dict | None = None
    equity: list[tuple[datetime, float]] = []
    realized = 0.0

    for i in range(len(bars)):
        window = bars.iloc[: i + 1]
        now = pd.Timestamp(window.index[-1]).to_pydatetime()
        price = float(window["close"].iloc[-1])

        if i >= warmup:
            ctx = Context(symbol=symbol, now=now, bars=window, position=position, params=strategy.params)
            sig = strategy.on_bar(ctx)
            if sig is not None:
                if sig.side == "BUY" and position.is_flat:
                    qty = int(sig.qty or strategy.params.get("qty", 100))
                    position = Position(qty=qty, avg_price=price)
                    pending_entry = {"ts": now, "price": price, "qty": qty, "reason": sig.reason}
                    realized -= commission_per_trade
                elif sig.side in ("EXIT", "SELL") and position.is_long and pending_entry:
                    pnl = (price - position.avg_price) * position.qty - commission_per_trade
                    realized += (price - position.avg_price) * position.qty - commission_per_trade
                    trades.append(
                        Trade(
                            entry_ts=pending_entry["ts"],
                            entry_price=pending_entry["price"],
                            exit_ts=now,
                            exit_price=price,
                            qty=position.qty,
                            pnl=pnl,
                            return_pct=(price / position.avg_price - 1) * 100,
                            reason_in=pending_entry["reason"],
                            reason_out=sig.reason,
                        )
                    )
                    position = Position()
                    pending_entry = None

        unrealized = (price - position.avg_price) * position.qty if position.is_long else 0.0
        equity.append((now, realized + unrealized))

    eq = pd.Series({t: v for t, v in equity}, name="equity")
    return BacktestResult(
        symbol=symbol,
        timeframe=strategy.timeframe,
        trades=trades,
        equity_curve=eq,
        metrics=_metrics(trades, eq),
    )


def _metrics(trades: list[Trade], equity: pd.Series) -> dict:
    n = len(trades)
    if n == 0:
        return {"trades": 0, "note": "約定なし"}
    pnls = [t.pnl for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gross_win = sum(wins)
    gross_loss = -sum(losses)
    peak = equity.cummax()
    dd = (equity - peak)
    return {
        "trades": n,
        "win_rate_pct": round(100 * len(wins) / n, 1),
        "total_pnl": round(sum(pnls), 0),
        "avg_pnl": round(sum(pnls) / n, 0),
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else None,
        "max_drawdown": round(float(dd.min()), 0),
        "best": round(max(pnls), 0),
        "worst": round(min(pnls), 0),
    }
