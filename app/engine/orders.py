"""実発注（P4）のキュー管理。backend <-> bridge の非同期リレーの backend 側。

流れ:
  live.py で mode="live" のシグナルが RiskEngine.check() を通過したら queue_order() で
  Order(status="new") を作る（この時点では実発注していない）。
    -> bridge が GET /api/orders/pending でこの行を拾い、status="sending" にする
    -> bridge が Excel の RssStockOrder に書き込み・発注トリガーを立てる
    -> セルの表示が確定したら bridge が POST /api/orders/{id}/report で結果を報告
    -> apply_report() が Order.status / broker_order_id / filled_qty / avg_price を更新

建玉の扱い（約定の自動確認＝RssOrderStatus 追跡は未実装のため）:
  - new / sending は「発注中」。決着がつくまで同一戦略/銘柄への新規発注をブロックする。
  - sent（RSS が受理）は成行なので「ref_price（シグナル時点の価格）で約定した」とみなす。
    建値は ref_price。これで損切り/利確・大引け手仕舞い・戦略の EXIT が次の発注として出せる。
    EXIT/SELL が受理されたら (ref_price - 建値) × 数量 を日次損失リミットに加算する。
  - rejected / cancelled は「発注されなかった」として建玉に数えない。
  - timeout / error は結果が不明（実は発注されているかもしれない）ので、建玉には数えないが
    RiskEngine を DISARM して以降の自動発注を止める。MarketSpeed II の注文照会で確認してから
    手動で ARM し直す。
"""
from __future__ import annotations

from datetime import timedelta

from sqlmodel import Session, select

from app.config import get_config
from app.engine.risk import get_risk_engine
from app.models import Fill, Order, Strategy, utcnow
from app.strategy.base import Position

TERMINAL_FAILURE = {"rejected", "cancelled", "error", "timeout"}
UNKNOWN_RESULT = {"error", "timeout"}  # 発注されたか不明 → DISARM して人が確認する
TERMINAL_OK = {"filled"}
EXECUTED = {"sent", "filled"}  # 約定した（とみなす）＝建玉に反映する
IN_FLIGHT = {"new", "sending"}  # 発注中＝決着まで次の発注をブロック
OPEN_SIDES = {"BUY": 1, "SHORT": -1}  # 新規建て（値は方向）
CLOSE_SIDES = {"EXIT", "SELL", "COVER"}


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
    ref_price: float = 0.0,
    trade_type: str = "cash",
    margin_type: int = 0,
    open_date: int = 0,
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
        ref_price=ref_price,
        trade_type=trade_type,
        margin_type=margin_type,
        open_date=open_date,
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
    """Order 履歴から現在の建玉を畳む。発注中（new/sending）の注文も「約定する」扱いにして
    二重発注を避ける（安全側に倒す）。建値は約定価格、無ければ ref_price。"""
    rows = session.exec(
        select(Order)
        .where(Order.strategy_id == strategy_id, Order.symbol_code == symbol_code)
        .order_by(Order.ts, Order.id)
    ).all()
    pos = Position()
    for o in rows:
        if o.status in TERMINAL_FAILURE:
            continue
        if o.side in OPEN_SIDES and pos.is_flat:
            qty = o.filled_qty or o.qty or qty_hint
            pos = Position(qty=qty * OPEN_SIDES[o.side], avg_price=_exec_price(o))
        elif o.side in ("EXIT", "SELL") and pos.is_long:
            pos = Position()
        elif o.side in ("EXIT", "COVER") and pos.is_short:
            pos = Position()
    return pos


def last_open_order(session: Session, strategy_id: int, symbol_code: str) -> Order | None:
    """今の建玉を建てた注文（手仕舞い時に取引区分・信用区分を合わせるため）。"""
    return session.exec(
        select(Order)
        .where(
            Order.strategy_id == strategy_id,
            Order.symbol_code == symbol_code,
            Order.side.in_(tuple(OPEN_SIDES)),
            Order.status.not_in(tuple(TERMINAL_FAILURE)),
        )
        .order_by(Order.id.desc())
    ).first()


def _exec_price(o: Order) -> float:
    return o.avg_price or o.ref_price or 0.0


def has_in_flight_order(session: Session, strategy_id: int, symbol_code: str) -> bool:
    """発注中（new/sending）の注文が残っているか。残っていれば新規発注しない。"""
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
    """bridge からの結果報告を Order に反映する。約定なら Fill も記録。

    EXIT/SELL が初めて約定（とみなす）状態になったら実現損益を日次損失リミットに加算し、
    結果不明（timeout/error）なら RiskEngine を DISARM する。
    """
    order = session.get(Order, order_id)
    if order is None:
        return None
    newly_executed = status in EXECUTED and order.status not in EXECUTED
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

    if newly_executed and order.side in CLOSE_SIDES:
        pnl = _realized_pnl(session, order)
        if pnl is not None:
            today_jst = (utcnow() + timedelta(hours=9)).date()
            get_risk_engine().record_fill_pnl(pnl, today=today_jst)
    if status in UNKNOWN_RESULT:
        get_risk_engine().disarm(
            f"注文#{order.id} の結果が不明（{status}）— MarketSpeed II の注文照会を確認してから ARM し直す"
        )
    return order


def _realized_pnl(session: Session, exit_order: Order) -> float | None:
    """手仕舞い注文に対応する直前の新規建て（約定とみなしたもの）との損益。"""
    entry = session.exec(
        select(Order)
        .where(
            Order.strategy_id == exit_order.strategy_id,
            Order.symbol_code == exit_order.symbol_code,
            Order.side.in_(tuple(OPEN_SIDES)),
            Order.status.in_(EXECUTED),
            Order.id < exit_order.id,
        )
        .order_by(Order.id.desc())
    ).first()
    if entry is None or not _exec_price(entry) or not _exec_price(exit_order):
        return None
    qty = exit_order.filled_qty or exit_order.qty
    return (_exec_price(exit_order) - _exec_price(entry)) * qty * OPEN_SIDES[entry.side]


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
    """bridge に渡す発注指令。RssStockOrder / RssMargin*Order の引数に必要な最小限。"""
    return {
        "id": o.id,
        "symbol_code": o.symbol_code,
        "side": o.side,
        "qty": o.qty,
        "order_type": o.order_type,
        "limit_price": o.limit_price,
        "account_type": o.account_type,
        "trade_type": o.trade_type or "cash",
        "margin_type": o.margin_type or 0,
        "open_date": o.open_date or 0,
    }


def jst_yyyymmdd(ts) -> int:
    """naive UTC の datetime → JST の日付を yyyymmdd の整数で（RSS の建日と同じ形）。"""
    d = (ts + timedelta(hours=9)).date()
    return d.year * 10000 + d.month * 100 + d.day
