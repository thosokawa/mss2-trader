"""実発注（P4）のキュー管理。backend <-> bridge の非同期リレーの backend 側。

流れ:
  live.py で mode="live" のシグナルが RiskEngine.check() を通過したら queue_order() で
  Order(status="new") を作る（この時点では実発注していない）。
    -> bridge が GET /api/orders/pending でこの行を拾い、status="sending" にする
    -> bridge が Excel の RssStockOrder に書き込み・発注トリガーを立てる
    -> セルの表示が確定したら bridge が POST /api/orders/{id}/report で結果を報告
    -> apply_report() が Order.status / broker_order_id / filled_qty / avg_price を更新

未終端の Order（rejected/cancelled/error/timeout 以外）は「建玉が生きている/発注中」
として扱う — 二重発注を避けるため、決着がつくまでは同一戦略/銘柄への新規発注をブロックする。
"""
from __future__ import annotations

from sqlmodel import Session, select

from app.config import get_config
from app.models import Fill, Order, Strategy, utcnow
from app.strategy.base import Position

TERMINAL_FAILURE = {"rejected", "cancelled", "error", "timeout"}
TERMINAL_OK = {"filled"}
IN_FLIGHT = {"new", "sending", "sent"}  # 決着待ち＝建玉/発注が生きている扱い


def queue_order(
    session: Session,
    strat_row: Strategy,
    symbol_code: str,
    side: str,
    qty: int,
    reason: str,
    *,
    order_type: str = "MKT",
    limit_price: float | None = None,
    account_type: str | None = None,
    idempotency_key: str = "",
) -> Order:
    order = Order(
        strategy_id=strat_row.id,
        strategy_name=strat_row.name,
        symbol_code=symbol_code,
        side=side,
        qty=qty,
        order_type=order_type,
        limit_price=limit_price,
        account_type=account_type or get_config().trading.default_account_type,
        reason=reason,
        status="new",
        idempotency_key=idempotency_key,
    )
    session.add(order)
    session.commit()
    session.refresh(order)
    return order


def current_live_position(
    session: Session, strategy_id: int, symbol_code: str, qty_hint: int = 100
) -> Position:
    """Order 履歴から現在の建玉を畳む。決着がついていない発注も「生きている」扱いにして
    二重発注を避ける（安全側に倒す）。"""
    rows = session.exec(
        select(Order)
        .where(Order.strategy_id == strategy_id, Order.symbol_code == symbol_code)
        .order_by(Order.ts, Order.id)
    ).all()
    pos = Position()
    for o in rows:
        if o.status in TERMINAL_FAILURE:
            continue
        if o.side == "BUY" and pos.is_flat:
            qty = o.filled_qty or o.qty or qty_hint
            price = o.avg_price or 0.0
            pos = Position(qty=qty, avg_price=price)
        elif o.side in ("EXIT", "SELL") and pos.is_long:
            pos = Position()
    return pos


def has_in_flight_order(session: Session, strategy_id: int, symbol_code: str) -> bool:
    """未決着（new/sending/sent）の発注が残っているか。残っていれば新規発注しない。"""
    row = session.exec(
        select(Order).where(
            Order.strategy_id == strategy_id,
            Order.symbol_code == symbol_code,
            Order.status.in_(IN_FLIGHT),
        )
    ).first()
    return row is not None


def apply_report(
    session: Session,
    order_id: int,
    *,
    status: str,
    broker_order_id: str = "",
    filled_qty: int = 0,
    avg_price: float = 0.0,
    error: str = "",
) -> Order | None:
    """bridge からの結果報告を Order に反映する。約定なら Fill も記録。"""
    order = session.get(Order, order_id)
    if order is None:
        return None
    order.status = status
    if broker_order_id:
        order.broker_order_id = broker_order_id
    if filled_qty:
        order.filled_qty = filled_qty
    if avg_price:
        order.avg_price = avg_price
    if error:
        order.error = error
    order.updated_at = utcnow()
    session.add(order)
    if status == "filled" and filled_qty:
        session.add(Fill(order_id=order.id, qty=filled_qty, price=avg_price))
    session.commit()
    session.refresh(order)
    return order


def claim_pending(session: Session, limit: int = 20) -> list[Order]:
    """bridge が拾うぶんの新規注文を「sending」にマークして返す（一度きり配布）。"""
    rows = session.exec(
        select(Order).where(Order.status == "new").order_by(Order.ts, Order.id).limit(limit)
    ).all()
    now = utcnow()
    for o in rows:
        o.status = "sending"
        o.updated_at = now
        session.add(o)
    session.commit()
    for o in rows:
        session.refresh(o)
    return rows


def order_to_dict(o: Order) -> dict:
    """bridge に渡す発注指令。RssStockOrder の引数に必要な最小限。"""
    return {
        "id": o.id,
        "symbol_code": o.symbol_code,
        "side": o.side,
        "qty": o.qty,
        "order_type": o.order_type,
        "limit_price": o.limit_price,
        "account_type": o.account_type,
    }
