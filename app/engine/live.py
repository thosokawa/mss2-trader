"""ライブのシグナル生成ループ（P2）。

流れ:
  bridge から届いた tick は aggregator が確定足（Bar）にしている。
  live エンジンはそれを読んで、enabled な Strategy ごとに:
    対象銘柄セットの各銘柄の「まだ評価していない確定足」を on_bar に流す
    -> Signal が返ったら:
       - Signal を DB 保存（origin="live", idempotency_key で重複防止）
       - notify.send_slack で通知
  ポジションは live シグナルの履歴から復元する（BUY で建て、EXIT/SELL で手仕舞い）。
  mode=="live" の実発注は P4（ここではまだやらない）。

バックテストと同じ Strategy.on_bar を呼ぶので挙動が一致する。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime

import pandas as pd
from sqlmodel import Session, select

from app.bars import load_bars
from app.models import LiveCursor, Signal, Strategy, Symbol, SymbolSetItem, utcnow
from app.notify import format_signal, send_slack
from app.strategy.base import Context, Position
from app.strategy.registry import load_strategy_class

log = logging.getLogger("live")


def evaluate_latest(strategy, bars: pd.DataFrame, symbol: str, position: Position | None = None):
    """確定済み足の列に対して最新1本ぶんの判定を返す。単発評価ユーティリティ。"""
    if bars.empty:
        return None
    now = pd.Timestamp(bars.index[-1]).to_pydatetime()
    ctx = Context(symbol=symbol, now=now, bars=bars, position=position or Position(), params=strategy.params)
    return strategy.on_bar(ctx)


def idempotency_key(strategy_name: str, symbol: str, bar_ts: datetime) -> str:
    return f"{strategy_name}:{symbol}:{bar_ts.isoformat()}"


def position_from_signals(
    session: Session, strategy_id: int, symbol_code: str, qty_hint: int = 100
) -> Position:
    """live シグナル履歴を畳んで現在ポジションを求める（現物ロング only）。"""
    rows = session.exec(
        select(Signal)
        .where(
            Signal.strategy_id == strategy_id,
            Signal.symbol_code == symbol_code,
            Signal.origin == "live",
        )
        .order_by(Signal.ts, Signal.id)
    ).all()
    pos = Position()
    for s in rows:
        if s.side == "BUY" and pos.is_flat:
            pos = Position(qty=qty_hint, avg_price=s.price)
        elif s.side in ("EXIT", "SELL") and pos.is_long:
            pos = Position()
    return pos


def _symbols_for(session: Session, strategy: Strategy) -> list[str]:
    if strategy.symbol_set_id is None:
        return []
    return list(
        session.exec(
            select(SymbolSetItem.symbol_code)
            .where(SymbolSetItem.set_id == strategy.symbol_set_id)
            .order_by(SymbolSetItem.sort_order, SymbolSetItem.id)
        ).all()
    )


def _cursor(session: Session, strategy_id: int, symbol_code: str) -> LiveCursor | None:
    return session.exec(
        select(LiveCursor).where(
            LiveCursor.strategy_id == strategy_id, LiveCursor.symbol_code == symbol_code
        )
    ).first()


def _run_strategy_symbol(
    session: Session,
    strat_row: Strategy,
    strat,
    symbol_code: str,
    *,
    notify: bool,
) -> list[Signal]:
    tf = strat_row.timeframe
    bars = load_bars(session, symbol_code, tf)
    if bars.empty:
        return []

    latest_ts = pd.Timestamp(bars.index[-1]).to_pydatetime()
    cur = _cursor(session, strat_row.id, symbol_code)
    if cur is None:
        # 初回：発火させず、最新確定足まで見たことにするだけ。
        session.add(LiveCursor(strategy_id=strat_row.id, symbol_code=symbol_code, last_bar_ts=latest_ts))
        session.commit()
        return []
    if latest_ts <= cur.last_bar_ts:
        return []

    qty_hint = int(strat.params.get("qty", 100))
    pos = position_from_signals(session, strat_row.id, symbol_code, qty_hint)
    name = getattr(session.get(Symbol, symbol_code), "name", "") or ""

    all_ts = [pd.Timestamp(t).to_pydatetime() for t in bars.index]
    new_ts = [t for t in all_ts if t > cur.last_bar_ts]
    fired: list[Signal] = []
    for ts in new_ts:
        window = bars.loc[:ts]
        ctx = Context(symbol=symbol_code, now=ts, bars=window, position=pos, params=strat.params)
        sig = strat.on_bar(ctx)
        if sig is None:
            continue
        key = idempotency_key(strat_row.name, symbol_code, ts)
        if session.exec(select(Signal).where(Signal.idempotency_key == key)).first():
            continue
        price = float(window["close"].iloc[-1])
        row = Signal(
            strategy_id=strat_row.id,
            strategy_name=strat_row.name,
            symbol_code=symbol_code,
            ts=ts,
            side=sig.side,
            reason=sig.reason,
            price=price,
            origin="live",
            idempotency_key=key,
        )
        session.add(row)
        session.commit()
        fired.append(row)
        if sig.side == "BUY":
            pos = Position(qty=qty_hint, avg_price=price)
        elif sig.side in ("EXIT", "SELL"):
            pos = Position()
        if notify:
            send_slack(format_signal(strat_row.name, symbol_code, name, sig.side, price, sig.reason))

    cur.last_bar_ts = latest_ts
    cur.updated_at = utcnow()
    session.add(cur)
    session.commit()
    return fired


def run_once(session: Session, *, notify: bool = True) -> list[Signal]:
    """enabled な全 Strategy を1周評価する。生成した live Signal を返す。"""
    strategies = session.exec(select(Strategy).where(Strategy.enabled == True)).all()  # noqa: E712
    out: list[Signal] = []
    for strat_row in strategies:
        try:
            cls = load_strategy_class(strat_row.class_path)
            params = json.loads(strat_row.params_json or "{}")
        except Exception:  # noqa: BLE001
            log.exception("戦略ロード失敗: %s (%s)", strat_row.name, strat_row.class_path)
            continue
        strat = cls(params)
        strat.timeframe = strat_row.timeframe
        for symbol_code in _symbols_for(session, strat_row):
            try:
                out.extend(_run_strategy_symbol(session, strat_row, strat, symbol_code, notify=notify))
            except Exception:  # noqa: BLE001
                log.exception("live 評価失敗: %s / %s", strat_row.name, symbol_code)
    return out
