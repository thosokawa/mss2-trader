"""tick 列 <-> ローソク足の変換、および DB からの足読み出し。"""
from __future__ import annotations

from datetime import datetime

import pandas as pd
from sqlmodel import Session, select

from app.models import Bar

TIMEFRAME_TO_PANDAS = {"1m": "1min", "5m": "5min", "15m": "15min", "1d": "1D"}


def ticks_to_bars(ticks: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    """ticks: index=DatetimeIndex, 列 price, volume(任意)。戻り値: o/h/l/c/volume。

    最後の（未確定の可能性がある）足も含めて返す。確定判定は呼び出し側で行う。
    """
    rule = TIMEFRAME_TO_PANDAS[timeframe]
    price = ticks["price"].resample(rule, label="left", closed="left")
    out = price.ohlc()
    out.columns = ["open", "high", "low", "close"]
    if "volume" in ticks:
        out["volume"] = ticks["volume"].resample(rule, label="left", closed="left").sum()
    else:
        out["volume"] = 0.0
    return out.dropna(subset=["open"])


def load_bars(
    session: Session,
    symbol_code: str,
    timeframe: str,
    start: datetime | None = None,
    end: datetime | None = None,
) -> pd.DataFrame:
    """DB の Bar を pandas DataFrame（index=ts, 列 open/high/low/close/volume）で返す。"""
    stmt = select(Bar).where(Bar.symbol_code == symbol_code, Bar.timeframe == timeframe)
    if start:
        stmt = stmt.where(Bar.ts >= start)
    if end:
        stmt = stmt.where(Bar.ts <= end)
    stmt = stmt.order_by(Bar.ts)
    rows = session.exec(stmt).all()
    if not rows:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    df = pd.DataFrame(
        [
            {
                "ts": r.ts,
                "open": r.open,
                "high": r.high,
                "low": r.low,
                "close": r.close,
                "volume": r.volume,
            }
            for r in rows
        ]
    ).set_index("ts")
    df.index = pd.to_datetime(df.index)
    return df


def upsert_bars(session: Session, symbol_code: str, timeframe: str, df: pd.DataFrame, source: str) -> int:
    """(symbol, timeframe, ts) をキーに Bar を upsert。追加/更新した件数を返す。"""
    existing = {
        b.ts: b
        for b in session.exec(
            select(Bar).where(Bar.symbol_code == symbol_code, Bar.timeframe == timeframe)
        ).all()
    }
    n = 0
    for ts, row in df.iterrows():
        ts = pd.Timestamp(ts).to_pydatetime()
        b = existing.get(ts)
        if b is None:
            b = Bar(symbol_code=symbol_code, timeframe=timeframe, ts=ts, open=0, high=0, low=0, close=0)
        b.open = float(row["open"])
        b.high = float(row["high"])
        b.low = float(row["low"])
        b.close = float(row["close"])
        b.volume = float(row.get("volume", 0) or 0)
        b.source = source
        session.add(b)
        n += 1
    session.commit()
    return n
