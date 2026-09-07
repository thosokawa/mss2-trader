"""ペーパートレード（P3）。

mode=paper の戦略が live エンジンでシグナルを出したとき、PaperBroker で擬似約定し
PaperTrade（1往復）を記録する。実発注は一切しない。

- BUY  : 建玉が無ければ開く
- EXIT / SELL : 建玉があれば仕切って損益確定
ポジションは PaperTrade（status=open）から復元する。
"""
from __future__ import annotations

import logging
from datetime import datetime

from sqlmodel import Session, select

from app.config import get_config
from app.engine.broker import PaperBroker
from app.models import Bar, PaperTrade, Strategy, Tick
from app.strategy.base import Position, Signal

log = logging.getLogger("paper")


def _open_trade(session: Session, strategy_id: int, symbol_code: str) -> PaperTrade | None:
    return session.exec(
        select(PaperTrade).where(
            PaperTrade.strategy_id == strategy_id,
            PaperTrade.symbol_code == symbol_code,
            PaperTrade.status == "open",
        )
    ).first()


def current_position(session: Session, strategy_id: int, symbol_code: str) -> Position:
    t = _open_trade(session, strategy_id, symbol_code)
    return Position(qty=t.qty, avg_price=t.entry_price) if t else Position()


def latest_price(session: Session, symbol_code: str) -> float | None:
    """含み損益の評価に使う最新価格。ティック優先、無ければ最新の確定足の終値。"""
    t = session.exec(
        select(Tick)
        .where(Tick.symbol_code == symbol_code, Tick.price > 0)
        .order_by(Tick.ts.desc())
    ).first()
    if t:
        return t.price
    b = session.exec(
        select(Bar).where(Bar.symbol_code == symbol_code).order_by(Bar.ts.desc())
    ).first()
    return b.close if b else None


def on_signal(
    session: Session,
    strat_row: Strategy,
    symbol_code: str,
    side: str,
    ref_price: float,
    reason: str,
    ts: datetime,
    qty_hint: int,
) -> PaperTrade | None:
    """シグナル1件をペーパー約定する。開いた/閉じた PaperTrade を返す（何もしなければ None）。"""
    broker = PaperBroker(slippage_bps=get_config().paper.slippage_bps)
    open_t = _open_trade(session, strat_row.id, symbol_code)

    if side == "BUY":
        if open_t:
            return None
        res = broker.place(symbol_code, Signal("BUY", qty_hint, reason), ref_price)
        t = PaperTrade(
            strategy_id=strat_row.id,
            strategy_name=strat_row.name,
            symbol_code=symbol_code,
            qty=res.filled_qty,
            entry_ts=ts,
            entry_price=res.avg_price,
            entry_reason=reason,
            status="open",
        )
        session.add(t)
        session.commit()
        session.refresh(t)
        log.info("paper OPEN  %s %s x%d @%.1f", strat_row.name, symbol_code, res.filled_qty, res.avg_price)
        return t

    if side in ("EXIT", "SELL"):
        if not open_t:
            return None
        res = broker.place(symbol_code, Signal("SELL", open_t.qty, reason), ref_price)
        open_t.exit_ts = ts
        open_t.exit_price = res.avg_price
        open_t.exit_reason = reason
        open_t.pnl = (res.avg_price - open_t.entry_price) * open_t.qty
        open_t.return_pct = (
            (res.avg_price / open_t.entry_price - 1) * 100 if open_t.entry_price else 0.0
        )
        open_t.status = "closed"
        session.add(open_t)
        session.commit()
        log.info("paper CLOSE %s %s pnl=%.0f", strat_row.name, symbol_code, open_t.pnl)
        return open_t

    return None


def summarize(closed: list[PaperTrade]) -> dict:
    """確定済み PaperTrade のリストから成績指標を作る（backtest._metrics のペーパー版）。"""
    n = len(closed)
    if n == 0:
        return {"trades": 0}
    pnls = [t.pnl or 0.0 for t in closed]
    wins = [p for p in pnls if p > 0]
    gross_win = sum(wins)
    gross_loss = -sum(p for p in pnls if p <= 0)
    return {
        "trades": n,
        "win_rate_pct": round(100 * len(wins) / n, 1),
        "realized_pnl": round(sum(pnls), 0),
        "avg_pnl": round(sum(pnls) / n, 0),
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else None,
        "best": round(max(pnls), 0),
        "worst": round(min(pnls), 0),
    }
