"""過去足に対して Strategy.on_bar() を1本ずつ流し、擬似的に売買してパフォーマンスを測る。

割り切り:
- 買い（BUY）と売建（SHORT）。どちらを建てるかは売買方向 direction（買いのみ/売りのみ/両方）。
  同時に持つ建玉は1つ（ドテンはしない）
- 約定は「シグナルが出た足の終値」で成立（スリッページ/板は考慮しない）
- 手数料は commission_per_trade（片道・円）で概算。信用の金利・貸株料は考慮しない
- 損益は (手仕舞い値 - 建値) × 株数 × 方向（ロング +1 / ショート -1）

戦略パラメータに `stop_loss_pct` / `take_profit_pct`（建値からの%）があれば、
on_bar の判断より優先してその足の高値/安値でストップ・ターゲット判定する
（app/engine/stops.py。ショートは向きが逆）。

`hold_overnight=False` なら日中足では大引け前の最後の足の終値で手仕舞いし、
その足では新規建てしない（app/engine/eod.py）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import pandas as pd

from app.engine.eod import flatten_at_close, is_last_bar_of_day
from app.engine.stops import StopTargetHit, check_stop_target
from app.strategy.base import Context, Position, Strategy

OPEN_SIDES = {"BUY": 1, "SHORT": -1}
CLOSE_SIDES = {"EXIT", "SELL", "COVER"}


@dataclass
class Trade:
    entry_ts: datetime
    entry_price: float
    exit_ts: datetime
    exit_price: float
    qty: int  # 株数（常に正）
    pnl: float
    return_pct: float
    reason_in: str = ""
    reason_out: str = ""
    side: str = "LONG"  # "LONG" | "SHORT"


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

    stop_loss_pct = strategy.params.get("stop_loss_pct")
    take_profit_pct = strategy.params.get("take_profit_pct")
    flatten_eod = flatten_at_close(strategy.params, strategy.timeframe)

    position = Position()
    trades: list[Trade] = []
    entry: dict | None = None
    equity: list[tuple[datetime, float]] = []
    realized = 0.0

    def close(now: datetime, price: float, reason: str) -> None:
        nonlocal position, entry, realized
        qty = abs(position.qty)
        gross = (price - position.avg_price) * qty * position.direction
        pnl = gross - commission_per_trade
        realized += pnl
        trades.append(
            Trade(
                entry_ts=entry["ts"],
                entry_price=entry["price"],
                exit_ts=now,
                exit_price=price,
                qty=qty,
                pnl=pnl,
                return_pct=(price / position.avg_price - 1) * 100 * position.direction,
                reason_in=entry["reason"],
                reason_out=reason,
                side="LONG" if position.is_long else "SHORT",
            )
        )
        position = Position()
        entry = None

    for i in range(len(bars)):
        window = bars.iloc[: i + 1]
        now = pd.Timestamp(window.index[-1]).to_pydatetime()
        price = float(window["close"].iloc[-1])
        bar_high = float(window["high"].iloc[-1])
        bar_low = float(window["low"].iloc[-1])
        eod = flatten_eod and is_last_bar_of_day(
            now,
            strategy.timeframe,
            pd.Timestamp(bars.index[i + 1]).to_pydatetime() if i + 1 < len(bars) else None,
        )

        if i >= warmup:
            holding = not position.is_flat and entry is not None
            hit = (
                check_stop_target(
                    position.avg_price, bar_high, bar_low,
                    stop_loss_pct=stop_loss_pct, take_profit_pct=take_profit_pct,
                    direction=position.direction,
                )
                if holding
                else None
            )
            if hit is None and eod and holding:
                hit = StopTargetHit(price, "大引け手仕舞い")
            if hit is not None:
                close(now, hit.price, hit.reason)
            else:
                ctx = Context(symbol=symbol, now=now, bars=window, position=position, params=strategy.params)
                sig = strategy.decide(ctx)
                if sig is not None:
                    if sig.side in OPEN_SIDES and position.is_flat and not eod:
                        qty = int(sig.qty or strategy.params.get("qty", 100))
                        position = Position(qty=qty * OPEN_SIDES[sig.side], avg_price=price)
                        entry = {"ts": now, "price": price, "reason": sig.reason}
                        realized -= commission_per_trade
                    elif sig.side in CLOSE_SIDES and holding:
                        # 旧来の SELL はロングの手仕舞いとしてだけ扱う
                        if not (sig.side == "SELL" and position.is_short):
                            close(now, price, sig.reason)

        unrealized = (price - position.avg_price) * abs(position.qty) * position.direction
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
