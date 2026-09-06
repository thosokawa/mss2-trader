from datetime import datetime, timedelta

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


def _make_strategy(s: Session, code: str, enabled: bool = True) -> Strategy:
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
        mode="notify",
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
