import pandas as pd

from app.bars import ticks_to_bars


def test_ticks_to_bars_5m():
    idx = pd.to_datetime(
        [
            "2026-01-05 09:00:10",
            "2026-01-05 09:01:00",
            "2026-01-05 09:04:59",
            "2026-01-05 09:05:30",
        ]
    )
    ticks = pd.DataFrame({"price": [100, 102, 101, 105], "volume": [10, 5, 5, 20]}, index=idx)
    bars = ticks_to_bars(ticks, "5m")
    assert len(bars) == 2
    first = bars.iloc[0]
    assert first["open"] == 100
    assert first["high"] == 102
    assert first["low"] == 100
    assert first["close"] == 101
    assert first["volume"] == 20


def test_upsert_twice_keeps_one_bar_per_time():
    """同じ足を2回書いても1本（2026-10-01、過去データの取得が同時に2回走って全部2本ずつになった）。"""
    from datetime import datetime

    import pandas as pd
    from sqlmodel import Session, func, select

    from app.bars import load_bars, upsert_bars
    from app.db import engine, init_db
    from app.models import Bar

    init_db()
    idx = pd.date_range("2026-01-08 00:00", periods=3, freq="5min")
    df = pd.DataFrame({"open": [1.0, 2, 3], "high": [1.0, 2, 3], "low": [1.0, 2, 3], "close": [1.0, 2, 3],
                       "volume": [10.0, 20, 30]}, index=idx)
    with Session(engine) as s:
        upsert_bars(s, "9994", "5m", df, source="yfinance")
        upsert_bars(s, "9994", "5m", df.assign(close=[1.5, 2.5, 3.5]), source="yfinance")
        n = s.exec(select(func.count()).select_from(Bar).where(Bar.symbol_code == "9994")).one()
        bars = load_bars(s, "9994", "5m")
    assert n == 3 and list(bars["close"]) == [1.5, 2.5, 3.5]
    assert bars.index[0] == datetime(2026, 1, 8, 0, 0)


def test_init_db_removes_existing_duplicates():
    from datetime import datetime

    from sqlmodel import Session, func, select

    from app.db import engine, init_db
    from app.models import Bar

    init_db()
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP INDEX IF EXISTS ux_bar_key")  # 一意インデックスが無かった頃の DB を再現
    with Session(engine) as s:
        for close in (1.0, 2.0):
            s.add(Bar(symbol_code="9995", timeframe="5m", ts=datetime(2026, 1, 9), open=1, high=2, low=1,
                      close=close, volume=1))
        s.commit()
    init_db()
    with Session(engine) as s:
        rows = s.exec(select(Bar).where(Bar.symbol_code == "9995")).all()
        idx = s.exec(select(func.count()).select_from(Bar)).one()
    with engine.connect() as conn:
        names = {r[0] for r in conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='index'")}
    assert len(rows) == 1 and rows[0].close == 2.0  # 後から書いた方を残す
    assert "ux_bar_key" in names and idx > 0
