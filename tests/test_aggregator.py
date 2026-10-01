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


def test_sliding_window_does_not_truncate_old_bars():
    """15秒ごとに3時間の窓で作り直しても、窓の先頭で途中からの tick だけの足に上書きしない
    （2026-10-01 まで、どの足も最後の数秒だけの始値・高値・安値・出来高になっていた）。"""
    init_db()
    base = datetime(2026, 1, 6, 0, 0, 0)
    with Session(engine) as s:
        for i in range(0, 600, 2):  # 00:00〜00:10 に2秒ごと。価格は上下、出来高は増え続ける
            s.add(Tick(symbol_code="9992", ts=base + timedelta(seconds=i), price=100 + (i % 60) / 10,
                       volume=1000 + i * 10, received_at=base + timedelta(seconds=i)))
        s.commit()
        # 00:00〜00:05 の足が確定した直後に作る → 正しい足
        build_bars(s, timeframes=("5m",), lookback_minutes=180, now=base + timedelta(minutes=5, seconds=10))
        # 窓が進んで、00:00〜00:05 の足の途中が窓の先頭になったときにも作る（以前はここで上書きされた）
        for sec in (30, 120, 290):
            now = base + timedelta(minutes=5, seconds=sec)
            build_bars(s, timeframes=("5m",), lookback_minutes=5, now=now)
        b = s.exec(select(Bar).where(Bar.symbol_code == "9992", Bar.timeframe == "5m", Bar.ts == base)).one()
    assert (b.open, b.high, b.low) == (100.0, 105.8, 100.0)
    assert b.volume == 149 * 20  # 00:00:02〜00:04:58 の出来高の増加（先頭 tick は直前が無いので 0）


def test_rebuild_bars_repairs_from_ticks():
    from app.aggregator import rebuild_bars

    init_db()
    base = datetime(2026, 1, 7, 0, 0, 0)
    with Session(engine) as s:
        for i in range(0, 300, 2):
            s.add(Tick(symbol_code="9993", ts=base + timedelta(seconds=i), price=200 + (i % 50) / 10,
                       volume=5000 + i, received_at=base + timedelta(seconds=i)))
        s.add(Bar(symbol_code="9993", timeframe="5m", ts=base, open=204, high=204, low=204, close=204.8,
                  volume=2, source="rss"))  # 壊れた足
        s.commit()
        rebuild_bars(s, base - timedelta(hours=1), base + timedelta(hours=1), timeframes=("5m",))
        b = s.exec(select(Bar).where(Bar.symbol_code == "9993", Bar.timeframe == "5m", Bar.ts == base)).one()
    assert (b.open, b.high, b.low) == (200.0, 204.8, 200.0) and b.volume > 100
