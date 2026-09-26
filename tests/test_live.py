from datetime import datetime, timedelta

import pytest
from sqlmodel import Session, select

from app.db import engine, init_db
from app.engine import live
from app.models import Bar, Signal, Strategy, Symbol, SymbolSet, SymbolSetItem


def _add_bars(s: Session, code: str, closes: list[float], start: datetime, tf: str = "5m") -> None:
    for i, c in enumerate(closes):
        ts = start + timedelta(minutes=5 * i)
        s.add(
            Bar(
                symbol_code=code, timeframe=tf, ts=ts,
                open=c, high=c, low=c, close=c, volume=1000.0, source="rss",
            )
        )
    s.commit()


def _make_strategy(s: Session, code: str, enabled: bool = True, mode: str = "notify") -> Strategy:
    s.add(Symbol(code=code, name="テスト銘柄"))
    ss = SymbolSet(name=f"set-{code}")
    s.add(ss)
    s.commit()
    s.refresh(ss)
    s.add(SymbolSetItem(set_id=ss.id, symbol_code=code))
    st = Strategy(
        name=f"strat-{code}",
        class_path="app.strategy.examples.sma_cross:SmaCross",
        params_json='{"fast": 5, "slow": 20, "qty": 100}',
        symbol_set_id=ss.id,
        timeframe="5m",
        mode=mode,
        enabled=enabled,
    )
    s.add(st)
    s.commit()
    s.refresh(st)
    return st


DECLINE = [100 - i for i in range(30)]
RISE = [70 + i * 2 for i in range(20)]
BASE = datetime(2026, 3, 2, 0, 0, 0)


def test_first_run_initializes_cursor_without_firing():
    init_db()
    with Session(engine) as s:
        _make_strategy(s, "9800")
        _add_bars(s, "9800", DECLINE + RISE, BASE)  # クロスを含む全足を投入
        fired = live.run_once(s, notify=False)
        assert fired == []
        assert s.exec(select(Signal).where(Signal.symbol_code == "9800")).all() == []


def test_new_bars_after_enable_fire_once():
    init_db()
    with Session(engine) as s:
        st = _make_strategy(s, "9801")
        _add_bars(s, "9801", DECLINE, BASE)
        assert live.run_once(s, notify=False) == []  # カーソル初期化のみ

        _add_bars(s, "9801", RISE, BASE + timedelta(minutes=5 * len(DECLINE)))
        fired = live.run_once(s, notify=False)
        assert len(fired) >= 1
        first = fired[0]
        assert first.side == "BUY"
        assert first.origin == "live"
        assert first.idempotency_key

        # 再実行しても新たな足はないので発火しない
        assert live.run_once(s, notify=False) == []

        pos = live.position_from_signals(s, st.id, "9801")
        assert pos.is_long


def test_disabled_strategy_does_not_run():
    init_db()
    with Session(engine) as s:
        _make_strategy(s, "9802", enabled=False)
        _add_bars(s, "9802", DECLINE, BASE)
        live.run_once(s, notify=False)
        _add_bars(s, "9802", RISE, BASE + timedelta(minutes=5 * len(DECLINE)))
        fired = live.run_once(s, notify=False)
        assert [f for f in fired if f.symbol_code == "9802"] == []
        assert s.exec(select(Signal).where(Signal.symbol_code == "9802")).all() == []


def test_stop_loss_closes_paper_trade_before_natural_exit():
    init_db()
    from app.models import PaperTrade

    with Session(engine) as s:
        s.add(Symbol(code="9804", name="テスト銘柄"))
        ss = SymbolSet(name="set-9804")
        s.add(ss)
        s.commit()
        s.refresh(ss)
        s.add(SymbolSetItem(set_id=ss.id, symbol_code="9804"))
        st = Strategy(
            name="strat-9804",
            class_path="app.strategy.examples.sma_cross:SmaCross",
            params_json='{"fast": 5, "slow": 20, "qty": 100, "stop_loss_pct": 3.0}',
            symbol_set_id=ss.id,
            timeframe="5m",
            mode="paper",
            enabled=True,
        )
        s.add(st)
        s.commit()
        s.refresh(st)

        decline = [100 - i for i in range(30)]
        rise = [70 + i * 2 for i in range(7)]  # GC は最後の足（close=82）
        _add_bars(s, "9804", decline, BASE)
        live.run_once(s, notify=False)  # カーソル初期化

        base2 = BASE + timedelta(minutes=5 * len(decline))
        _add_bars(s, "9804", rise, base2)
        # GC 直後に急落する足を1本追加（低値がストップラインを割る）
        crash_ts = base2 + timedelta(minutes=5 * len(rise))
        s.add(Bar(symbol_code="9804", timeframe="5m", ts=crash_ts,
                  open=70.0, high=70.0, low=70.0, close=70.0, volume=1000.0, source="rss"))
        s.commit()

        fired = live.run_once(s, notify=False)
        sides = [f.side for f in fired]
        assert sides == ["BUY", "EXIT"]
        assert "損切り" in fired[-1].reason

        trades = s.exec(select(PaperTrade).where(PaperTrade.strategy_id == st.id)).all()
        assert len(trades) == 1
        t = trades[0]
        assert t.status == "closed"
        assert t.entry_price == pytest.approx(82.0 * 1.0003)  # PaperBroker の既定スリッページ込み
        assert t.exit_price < t.entry_price
        assert "損切り" in t.exit_reason
        assert live.paper.current_position(s, st.id, "9804").is_flat


def test_live_mode_blocked_by_default_creates_no_order():
    init_db()
    from app.models import Order

    with Session(engine) as s:
        st = _make_strategy(s, "9805", mode="live")
        _add_bars(s, "9805", DECLINE, BASE)
        live.run_once(s, notify=False)  # カーソル初期化

        _add_bars(s, "9805", RISE, BASE + timedelta(minutes=5 * len(DECLINE)))
        fired = live.run_once(s, notify=False)

        assert len(fired) >= 1
        assert fired[0].side == "BUY"
        assert "発注見送り" in fired[0].reason  # 既定は DISARMED + config.trading.enabled=false
        assert s.exec(select(Order).where(Order.strategy_id == st.id)).all() == []
        # ブロックされた分はポジションを進めていない
        from app.engine import orders as orders_mod
        assert orders_mod.current_live_position(s, st.id, "9805").is_flat


def test_live_mode_queues_order_when_armed():
    init_db()
    from unittest.mock import patch

    from app.config import TradingCfg
    from app.engine.risk import RiskEngine
    from app.models import Order

    session_base = datetime(2026, 3, 2, 9, 0, 0)  # 取引時間内(09:00-11:30)に収まるように
    armed_engine = RiskEngine(
        TradingCfg(enabled=True, max_qty_per_order=1000, max_notional_per_order=10_000_000,
                   daily_loss_limit=1_000_000, session_windows=["00:00-23:59"])
    )
    armed_engine.arm()

    with Session(engine) as s:
        st = _make_strategy(s, "9806", mode="live")
        _add_bars(s, "9806", DECLINE, session_base)
        with patch("app.engine.live.get_risk_engine", return_value=armed_engine):
            live.run_once(s, notify=False)  # カーソル初期化

            _add_bars(s, "9806", RISE, session_base + timedelta(minutes=5 * len(DECLINE)))
            fired = live.run_once(s, notify=False)

        assert len(fired) >= 1
        assert "発注見送り" not in fired[0].reason
        placed = s.exec(select(Order).where(Order.strategy_id == st.id)).all()
        assert len(placed) == 1
        assert placed[0].side == "BUY" and placed[0].status == "new" and placed[0].symbol_code == "9806"

        # 決着待ちのまま二重発注しない
        from app.engine import orders as orders_mod
        assert orders_mod.has_in_flight_order(s, st.id, "9806")
        assert orders_mod.current_live_position(s, st.id, "9806").is_long


def test_paper_mode_records_trades():
    init_db()
    from app.models import PaperTrade

    with Session(engine) as s:
        st = _make_strategy(s, "9803", mode="paper")
        _add_bars(s, "9803", DECLINE, BASE)
        live.run_once(s, notify=False)  # カーソル初期化

        _add_bars(s, "9803", RISE, BASE + timedelta(minutes=5 * len(DECLINE)))
        live.run_once(s, notify=False)

        trades = s.exec(select(PaperTrade).where(PaperTrade.strategy_id == st.id)).all()
        assert len(trades) >= 1
        assert trades[0].entry_price > 0
        # BUY で建玉ができ、live の ctx.position も PaperTrade 由来
        assert live.paper.current_position(s, st.id, "9803").is_long
