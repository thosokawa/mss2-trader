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
            "trade_type": "cash", "margin_type": 0,
        }


# ---- 受理（sent）を約定とみなす扱い -------------------------------------------------

import pytest  # noqa: E402

from app.config import TradingCfg  # noqa: E402
from app.engine.risk import RiskEngine  # noqa: E402


@pytest.fixture()
def risk(monkeypatch):
    """apply_report が触る RiskEngine をテストごとの使い捨てに差し替える。"""
    eng = RiskEngine(TradingCfg(enabled=True, daily_loss_limit=30_000))
    eng.arm()
    monkeypatch.setattr(orders, "get_risk_engine", lambda: eng)
    return eng


def test_sent_buy_is_position_at_ref_price_and_not_in_flight(risk):
    init_db()
    with Session(engine) as s:
        st = _strat(s, "7011")
        o = orders.queue_order(s, st, "7011", "BUY", 100, "GC", ref_price=500.0)
        orders.apply_report(s, o.id, status="sent", broker_order_id="発注済み(発注ID=1)")
        pos = orders.current_live_position(s, st.id, "7011")
        assert pos.is_long and pos.avg_price == 500.0
        # 受理済みなので次の発注（手仕舞い）をブロックしない
        assert orders.has_in_flight_order(s, st.id, "7011") is False


def test_exit_sent_records_realized_pnl_once(risk):
    init_db()
    with Session(engine) as s:
        st = _strat(s, "7012")
        buy = orders.queue_order(s, st, "7012", "BUY", 100, "GC", ref_price=1000.0)
        orders.apply_report(s, buy.id, status="sent")
        assert risk.state.day_realized_pnl == 0.0  # 買いでは損益は出ない

        ex = orders.queue_order(s, st, "7012", "EXIT", 100, "損切り", ref_price=980.0)
        orders.apply_report(s, ex.id, status="sent")
        assert risk.state.day_realized_pnl == pytest.approx(-2000.0)
        assert orders.current_live_position(s, st.id, "7012").is_flat

        # 後から filled が来ても二重に数えない
        orders.apply_report(s, ex.id, status="filled", filled_qty=100, avg_price=979.0)
        assert risk.state.day_realized_pnl == pytest.approx(-2000.0)
        assert risk.state.armed


def test_exit_loss_over_daily_limit_disarms(risk):
    init_db()
    with Session(engine) as s:
        st = _strat(s, "7013")
        buy = orders.queue_order(s, st, "7013", "BUY", 100, "GC", ref_price=3000.0)
        orders.apply_report(s, buy.id, status="sent")
        ex = orders.queue_order(s, st, "7013", "EXIT", 100, "損切り", ref_price=2600.0)
        orders.apply_report(s, ex.id, status="sent")  # -40,000 円 > 30,000 円
        assert not risk.state.armed
        assert "日次損失" in risk.state.halted_reason


@pytest.mark.parametrize("status", ["timeout", "error"])
def test_unknown_result_disarms(risk, status):
    init_db()
    with Session(engine) as s:
        st = _strat(s, f"7014{status}")
        o = orders.queue_order(s, st, f"7014{status}", "BUY", 100, "GC", ref_price=500.0)
        orders.apply_report(s, o.id, status=status, error="未確定のまま待機時間切れ")
        assert not risk.state.armed
        assert f"注文#{o.id}" in risk.state.halted_reason


def test_rejected_does_not_disarm(risk):
    init_db()
    with Session(engine) as s:
        st = _strat(s, "7015")
        o = orders.queue_order(s, st, "7015", "BUY", 100, "GC", ref_price=500.0)
        orders.apply_report(s, o.id, status="rejected", error="発注ロック中")
        assert risk.state.armed
