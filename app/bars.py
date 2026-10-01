"""tick 列 <-> ローソク足の変換、および DB からの足読み出し。"""
from __future__ import annotations

from datetime import datetime

import pandas as pd
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
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
    # 念のため同じ時刻の足は1本に（一意インデックスを作る前の DB でも、足が2回数えられないように）
    return df[~df.index.duplicated(keep="last")]


def upsert_bars(session: Session, symbol_code: str, timeframe: str, df: pd.DataFrame, source: str) -> int:
    """(symbol, timeframe, ts) をキーに Bar を upsert。追加/更新した件数を返す。

    SQLite の INSERT ... ON CONFLICT DO UPDATE で1行ずつ原子的に書く（一意インデックス ux_bar_key）。
    以前は「既存の足を読んでから足りない分を追加」だったので、同時に2回走ると同じ足が2本ずつできた。
    """
    if df.empty:
        return 0
    rows = [
        {
            "symbol_code": symbol_code, "timeframe": timeframe, "ts": pd.Timestamp(ts).to_pydatetime(),
            "open": float(r["open"]), "high": float(r["high"]), "low": float(r["low"]),
            "close": float(r["close"]), "volume": float(r.get("volume", 0) or 0), "source": source,
        }
        for ts, r in df.iterrows()
    ]
    stmt = sqlite_insert(Bar.__table__)
    stmt = stmt.on_conflict_do_update(
        index_elements=["symbol_code", "timeframe", "ts"],
        set_={k: stmt.excluded[k] for k in ("open", "high", "low", "close", "volume", "source")},
    )
    session.exec(stmt, params=rows)
    session.commit()
    return len(rows)
