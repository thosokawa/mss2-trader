from datetime import datetime, timedelta

import pytest
from sqlmodel import Session, select

from app.db import engine, init_db
from app.engine import live
from app.models import Bar, Signal, Strategy, Symbol
from app.strategy.base import Signal as StratSignal
from app.strategy.base import Strategy as BaseStrategy


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
    st = Strategy(
        name=f"strat-{code}",
        class_path="app.strategy.examples.sma_cross:SmaCross",
        params_json='{"fast": 5, "slow": 20, "qty": 100}',
        symbols=code,
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
        st = Strategy(
            name="strat-9804",
            class_path="app.strategy.examples.sma_cross:SmaCross",
            params_json='{"fast": 5, "slow": 20, "qty": 100, "stop_loss_pct": 3.0}',
            symbols="9804",
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

            # 実運用どおり足が1本確定するたびに評価する（古いシグナル扱いにしない）
            fired = []
            for i, c in enumerate(RISE):
                bar_start = session_base + timedelta(minutes=5 * (len(DECLINE) + i))
                _add_bars(s, "9806", [c], bar_start)
                fired += live.run_once(s, notify=False, now=bar_start + timedelta(minutes=5, seconds=30))

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


def test_live_mode_skips_stale_signal_after_downtime():
    """backend 停止後にまとめて評価された古い足のシグナルは発注しない。"""
    init_db()
    from unittest.mock import patch

    from app.config import TradingCfg
    from app.engine.risk import RiskEngine
    from app.models import Order

    session_base = datetime(2026, 3, 2, 9, 0, 0)
    armed_engine = RiskEngine(
        TradingCfg(enabled=True, max_qty_per_order=1000, max_notional_per_order=10_000_000,
                   daily_loss_limit=1_000_000, session_windows=["00:00-23:59"])
    )
    armed_engine.arm()

    with Session(engine) as s:
        st = _make_strategy(s, "9807", mode="live")
        _add_bars(s, "9807", DECLINE, session_base)
        with patch("app.engine.live.get_risk_engine", return_value=armed_engine):
            live.run_once(s, notify=False)  # カーソル初期化

            _add_bars(s, "9807", RISE, session_base + timedelta(minutes=5 * len(DECLINE)))
            # 最後の足の確定から1時間後に評価（停止していた backend が追いついた想定）
            last_bar_end = session_base + timedelta(minutes=5 * (len(DECLINE) + len(RISE)))
            fired = live.run_once(s, notify=False, now=last_bar_end + timedelta(hours=1))

        assert len(fired) >= 1
        assert all("古い足のシグナル" in f.reason for f in fired)
        assert s.exec(select(Order).where(Order.strategy_id == st.id)).all() == []


def test_live_mode_stop_loss_exit_is_ordered_after_buy_accepted():
    """買いが受理（sent）されたら、その後の損切りが売り注文として出る。"""
    init_db()
    from unittest.mock import patch

    from app.config import TradingCfg
    from app.engine import orders as orders_mod
    from app.engine.risk import RiskEngine
    from app.models import Order

    eng = RiskEngine(
        TradingCfg(enabled=True, max_qty_per_order=1000, max_notional_per_order=10_000_000,
                   daily_loss_limit=1_000_000, session_windows=["00:00-23:59"])
    )
    eng.arm()
    code = "9808"
    t0 = datetime(2026, 3, 2, 0, 0, 0)

    def bar(i: int, c: float) -> datetime:
        ts = t0 + timedelta(minutes=5 * i)
        s.add(Bar(symbol_code=code, timeframe="5m", ts=ts, open=c, high=c, low=c, close=c,
                  volume=1000.0, source="rss"))
        s.commit()
        return ts + timedelta(minutes=5, seconds=30)  # 確定直後に評価

    with Session(engine) as s, patch("app.engine.live.get_risk_engine", return_value=eng), \
            patch("app.engine.orders.get_risk_engine", return_value=eng):
        s.add(Symbol(code=code, name="テスト銘柄"))
        st = Strategy(
            name=f"strat-{code}", class_path="tests.test_eod:AlwaysBuy",
            params_json='{"qty": 100, "stop_loss_pct": 3}', symbols=code,
            timeframe="5m", mode="live", enabled=True,
        )
        s.add(st)
        s.commit()
        s.refresh(st)

        live.run_once(s, notify=False, now=bar(0, 1000.0))  # カーソル初期化
        live.run_once(s, notify=False, now=bar(1, 1000.0))  # BUY
        buy = s.exec(select(Order).where(Order.strategy_id == st.id)).one()
        assert buy.side == "BUY" and buy.ref_price == 1000.0

        # bridge が拾って受理を報告
        assert [o.id for o in orders_mod.claim_pending(s) if o.symbol_code == code] == [buy.id]
        orders_mod.apply_report(s, buy.id, status="sent", broker_order_id="発注済み")

        fired = live.run_once(s, notify=False, now=bar(2, 960.0))  # -4% → 損切り
        assert [f.side for f in fired] == ["EXIT"]
        assert "発注見送り" not in fired[0].reason
        exit_o = s.exec(
            select(Order).where(Order.strategy_id == st.id, Order.side == "EXIT")
        ).one()
        assert exit_o.status == "new" and exit_o.ref_price == pytest.approx(970.0)

        orders_mod.claim_pending(s)
        orders_mod.apply_report(s, exit_o.id, status="sent")
        assert eng.state.day_realized_pnl == pytest.approx(-3000.0)
        assert orders_mod.current_live_position(s, st.id, code).is_flat



class BuyEveryBar(BaseStrategy):
    """建玉に関係なく毎足 BUY を出す（ガードの検証用）。"""

    default_params = {"qty": 100}

    def on_bar(self, ctx):
        return StratSignal("BUY", reason="every bar")


def test_live_mode_blocks_buy_while_long():
    """建玉がある間の BUY シグナルは発注しない（買い増ししない）。"""
    init_db()
    from unittest.mock import patch

    from app.config import TradingCfg
    from app.engine.risk import RiskEngine
    from app.models import Order

    eng = RiskEngine(
        TradingCfg(enabled=True, max_qty_per_order=1000, max_notional_per_order=10_000_000,
                   daily_loss_limit=1_000_000, session_windows=["00:00-23:59"])
    )
    eng.arm()
    with Session(engine) as s, patch("app.engine.live.get_risk_engine", return_value=eng):
        st = _make_strategy(s, "9809", mode="live")
        st.class_path = "tests.test_live:BuyEveryBar"
        st.params_json = '{"qty": 100}'
        s.add(st)
        s.commit()
        _add_bars(s, "9809", [100.0], BASE)
        live.run_once(s, notify=False)  # カーソル初期化

        # 1本目: BUY 発注 → 受理された想定
        _add_bars(s, "9809", [101.0], BASE + timedelta(minutes=5))
        live.run_once(s, notify=False, now=BASE + timedelta(minutes=10, seconds=30))
        o = s.exec(select(Order).where(Order.strategy_id == st.id)).one()
        o.status = "sent"
        s.add(o)
        s.commit()

        # 2本目: また BUY が出るが建玉があるので発注しない
        _add_bars(s, "9809", [102.0], BASE + timedelta(minutes=10))
        fired = live.run_once(s, notify=False, now=BASE + timedelta(minutes=15, seconds=30))
        assert len(fired) == 1 and "建玉あり" in fired[0].reason
        assert len(s.exec(select(Order).where(Order.strategy_id == st.id)).all()) == 1
