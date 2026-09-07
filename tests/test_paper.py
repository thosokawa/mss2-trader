from datetime import datetime

import pytest
from sqlmodel import Session, select

from app.config import get_config
from app.db import engine, init_db
from app.engine import paper
from app.models import PaperTrade, Strategy


def _strat(s: Session, name: str) -> Strategy:
    st = Strategy(name=name, class_path="app.strategy.examples.sma_cross:SmaCross", mode="paper")
    s.add(st)
    s.commit()
    s.refresh(st)
    return st


BPS = None


def setup_module() -> None:
    global BPS
    BPS = get_config().paper.slippage_bps


def test_open_then_close_pnl():
    init_db()
    with Session(engine) as s:
        st = _strat(s, "paper-A")
        t0 = datetime(2026, 4, 1, 0, 0)
        t1 = datetime(2026, 4, 1, 1, 0)

        opened = paper.on_signal(s, st, "7203", "BUY", 1000.0, "GC", t0, 100)
        assert opened.status == "open"
        assert opened.entry_price == pytest.approx(1000.0 * (1 + BPS / 10_000))
        assert paper.current_position(s, st.id, "7203").is_long

        # 建玉があるので2度目の BUY は何もしない
        assert paper.on_signal(s, st, "7203", "BUY", 1010.0, "GC2", t0, 100) is None

        closed = paper.on_signal(s, st, "7203", "EXIT", 1100.0, "DC", t1, 100)
        assert closed.status == "closed"
        exit_px = 1100.0 * (1 - BPS / 10_000)
        assert closed.pnl == pytest.approx((exit_px - opened.entry_price) * 100)
        assert paper.current_position(s, st.id, "7203").is_flat


def test_exit_while_flat_is_noop():
    init_db()
    with Session(engine) as s:
        st = _strat(s, "paper-B")
        assert paper.on_signal(s, st, "6501", "EXIT", 900.0, "x", datetime(2026, 4, 2), 100) is None
        assert s.exec(select(PaperTrade).where(PaperTrade.strategy_id == st.id)).all() == []


def test_summarize():
    init_db()
    with Session(engine) as s:
        st = _strat(s, "paper-C")
        closed = [
            PaperTrade(strategy_id=st.id, symbol_code="X", qty=100, entry_ts=datetime(2026, 4, 3),
                       entry_price=100, exit_price=110, pnl=1000.0, status="closed"),
            PaperTrade(strategy_id=st.id, symbol_code="X", qty=100, entry_ts=datetime(2026, 4, 4),
                       entry_price=100, exit_price=95, pnl=-500.0, status="closed"),
        ]
        m = paper.summarize(closed)
    assert m["trades"] == 2
    assert m["win_rate_pct"] == 50.0
    assert m["realized_pnl"] == 500
    assert m["profit_factor"] == 2.0
