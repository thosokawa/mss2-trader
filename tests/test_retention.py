"""古い tick の定期削除（app/retention.py）。"""
from datetime import datetime, timedelta

from sqlmodel import Session, select

from app import retention
from app.db import engine, init_db
from app.models import Tick

NOW = datetime(2026, 11, 2, 8, 0)  # 月曜 17:00 JST（取引時間外）


def _ticks(code):
    with Session(engine) as s:
        return s.exec(select(Tick).where(Tick.symbol_code == code).order_by(Tick.ts)).all()


def test_prune_keeps_recent_and_latest_per_symbol():
    init_db()
    with Session(engine) as s:
        for d in (40, 35, 29, 1):  # 日前
            s.add(Tick(symbol_code="981A", ts=NOW - timedelta(days=d), price=100.0 + d, volume=1.0))
        # しばらく受信していない銘柄: 全部古いが、最新1件は残す
        for d in (50, 45):
            s.add(Tick(symbol_code="982A", ts=NOW - timedelta(days=d), price=200.0 + d, volume=1.0))
        s.commit()
    retention.prune_ticks(engine, 30, now=NOW)
    assert [round((NOW - t.ts).days) for t in _ticks("981A")] == [29, 1]
    left = _ticks("982A")
    assert len(left) == 1 and left[0].price == 245.0


def test_prune_runs_in_batches(monkeypatch):
    init_db()
    monkeypatch.setattr(retention, "BATCH", 3)
    with Session(engine) as s:
        for i in range(10):
            s.add(Tick(symbol_code="983A", ts=NOW - timedelta(days=60, minutes=i), price=1.0, volume=1.0))
        s.add(Tick(symbol_code="983A", ts=NOW, price=1.0, volume=1.0))
        s.commit()
    assert retention.prune_ticks(engine, 30, now=NOW) >= 10
    assert len(_ticks("983A")) == 1


def test_maybe_prune_only_outside_session_and_every_6h(monkeypatch):
    calls = []
    monkeypatch.setattr(retention, "prune_ticks", lambda e, d, now: calls.append(now) or 0)
    monkeypatch.setattr(retention, "_last_run", None)
    retention.maybe_prune(engine, 30, in_session=True, now=NOW)
    assert calls == []  # 取引時間中は消さない
    retention.maybe_prune(engine, 30, in_session=False, now=NOW)
    retention.maybe_prune(engine, 30, in_session=False, now=NOW + timedelta(hours=1))
    assert len(calls) == 1  # 6時間に1回
    retention.maybe_prune(engine, 30, in_session=False, now=NOW + timedelta(hours=7))
    assert len(calls) == 2
    retention.maybe_prune(engine, 0, in_session=False, now=NOW + timedelta(days=1))
    assert len(calls) == 2  # 0 日 = 消さない
