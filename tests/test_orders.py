from sqlmodel import Session, select

from app.db import engine, init_db
from app.engine import orders
from app.models import Fill, Order, Strategy


def _strat(s: Session, code: str) -> Strategy:
    st = Strategy(
        name=f"live-{code}",
        class_path="app.strategy.examples.sma_cross:SmaCross",
        mode="live",
    )
    s.add(st)
    s.commit()
    s.refresh(st)
    return st


def test_queue_order_creates_new_row():
    init_db()
    with Session(engine) as s:
        st = _strat(s, "7001")
        o = orders.queue_order(s, st, "7001", "BUY", 100, "GC", idempotency_key="k1")
        assert o.id is not None
        assert o.status == "new"
        assert o.strategy_name == st.name
        assert o.account_type == "0"


def test_claim_pending_marks_sending_and_is_one_shot():
    init_db()
    with Session(engine) as s:
        st = _strat(s, "7002")
        placed = orders.queue_order(s, st, "7002", "BUY", 100, "GC")
        claimed = [o for o in orders.claim_pending(s) if o.symbol_code == "7002"]
        assert len(claimed) == 1
        assert claimed[0].id == placed.id
        assert claimed[0].status == "sending"
        # 2回目の claim では（このテストが作った分は）もう拾われない（new のものだけが対象のため）
        assert [o for o in orders.claim_pending(s) if o.symbol_code == "7002"] == []


def test_apply_report_filled_creates_fill_and_updates_order():
    init_db()
    with Session(engine) as s:
        st = _strat(s, "7003")
        o = orders.queue_order(s, st, "7003", "BUY", 100, "GC")
        orders.apply_report(
            s, o.id, status="filled", broker_order_id="ORD123", filled_qty=100, avg_price=1234.5
        )
        refreshed = s.get(Order, o.id)
        assert refreshed.status == "filled"
        assert refreshed.broker_order_id == "ORD123"
        assert refreshed.filled_qty == 100
        assert refreshed.avg_price == 1234.5
        fills = s.exec(select(Fill).where(Fill.order_id == o.id)).all()
        assert len(fills) == 1
        assert fills[0].price == 1234.5


def test_apply_report_unknown_order_returns_none():
    init_db()
    with Session(engine) as s:
        assert orders.apply_report(s, 999999, status="filled") is None


def test_current_live_position_folds_buy_and_exit():
    init_db()
    with Session(engine) as s:
        st = _strat(s, "7004")
        buy = orders.queue_order(s, st, "7004", "BUY", 100, "GC")
        orders.apply_report(s, buy.id, status="filled", filled_qty=100, avg_price=1000.0)

        pos = orders.current_live_position(s, st.id, "7004")
        assert pos.is_long and pos.qty == 100 and pos.avg_price == 1000.0

        exit_o = orders.queue_order(s, st, "7004", "EXIT", 100, "DC")
        orders.apply_report(s, exit_o.id, status="filled", filled_qty=100, avg_price=1100.0)
        assert orders.current_live_position(s, st.id, "7004").is_flat


def test_current_live_position_treats_in_flight_as_alive():
    init_db()
    with Session(engine) as s:
        st = _strat(s, "7005")
        orders.queue_order(s, st, "7005", "BUY", 100, "GC")  # status="new" のまま
        pos = orders.current_live_position(s, st.id, "7005")
        assert pos.is_long  # 決着前でも二重発注を避けるため「建っている」扱い


def test_current_live_position_ignores_rejected():
    init_db()
    with Session(engine) as s:
        st = _strat(s, "7006")
        o = orders.queue_order(s, st, "7006", "BUY", 100, "GC")
        orders.apply_report(s, o.id, status="rejected", error="入力エラー")
        assert orders.current_live_position(s, st.id, "7006").is_flat


def test_has_in_flight_order():
    init_db()
    with Session(engine) as s:
        st = _strat(s, "7007")
        assert orders.has_in_flight_order(s, st.id, "7007") is False
        o = orders.queue_order(s, st, "7007", "BUY", 100, "GC")
        assert orders.has_in_flight_order(s, st.id, "7007") is True
        orders.apply_report(s, o.id, status="filled", filled_qty=100, avg_price=1000.0)
        assert orders.has_in_flight_order(s, st.id, "7007") is False


def test_order_to_dict_shape():
    init_db()
    with Session(engine) as s:
        st = _strat(s, "7008")
        o = orders.queue_order(s, st, "7008", "BUY", 100, "GC", account_type="1")
        d = orders.order_to_dict(o)
        assert d == {
            "id": o.id, "symbol_code": "7008", "side": "BUY", "qty": 100,
            "order_type": "MKT", "limit_price": None, "account_type": "1",
        }
