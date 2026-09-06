from datetime import UTC, datetime, timedelta

from sqlmodel import Session, select

from app.aggregator import build_bars
from app.db import engine, init_db
from app.models import Bar, Symbol, Tick


def test_build_bars_completed_only():
    init_db()
    base = datetime(2026, 1, 5, 0, 0, 0)  # naive UTC
    with Session(engine) as s:
        s.add(Symbol(code="9990"))
        for i in range(13):  # 00:00 .. 00:12
            s.add(
                Tick(
                    symbol_code="9990",
                    ts=base + timedelta(minutes=i),
                    price=100 + i,
                    volume=1000 * i,  # 累計出来高
                    received_at=base + timedelta(minutes=i),
                )
            )
        s.commit()

        # now = 00:12:30 → 00:00-05 と 00:05-10 は確定、00:10-15 は形成中
        now = (base + timedelta(minutes=12, seconds=30)).replace(tzinfo=UTC)
        n = build_bars(s, timeframes=("5m",), now=now)
        assert n == 2

        bars = s.exec(
            select(Bar).where(Bar.symbol_code == "9990", Bar.timeframe == "5m").order_by(Bar.ts)
        ).all()
    assert [b.ts for b in bars] == [base, base + timedelta(minutes=5)]
    first = bars[0]
    assert first.open == 100 and first.close == 104
    assert first.high == 104 and first.low == 100
    # 出来高は累計の階差合計（最初のtickの階差は0扱い）
    assert first.volume == 4000


def test_build_bars_idempotent():
    init_db()
    base = datetime(2026, 2, 2, 1, 0, 0)
    with Session(engine) as s:
        s.add(Symbol(code="9991"))
        for i in range(8):
            s.add(Tick(symbol_code="9991", ts=base + timedelta(minutes=i), price=200 + i,
                       volume=0, received_at=base + timedelta(minutes=i)))
        s.commit()
        now = (base + timedelta(minutes=10)).replace(tzinfo=UTC)
        first = build_bars(s, timeframes=("5m",), now=now)
        build_bars(s, timeframes=("5m",), now=now)
        bars = s.exec(select(Bar).where(Bar.symbol_code == "9991", Bar.timeframe == "5m")).all()
    # 01:00-05 と 01:05-10 の2本。2回流しても重複しない。
    assert first == 2
    assert len(bars) == 2
