"""tick（bridge から届く生の気配）を確定足に集約して Bar テーブルに書き込む。

- 入力: Tick.price（現在値）, Tick.volume（RSS の「出来高」= その日の累計）
- 出力: Bar（timeframe ごと）。**確定した足のみ** 書く（形成中の足は書かない）。
- 出来高は累計値の階差を足内で合計する。
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pandas as pd
from sqlmodel import Session, select

from app.bars import TIMEFRAME_TO_PANDAS, upsert_bars
from app.models import Tick

TF_DELTA = {
    "1m": timedelta(minutes=1),
    "5m": timedelta(minutes=5),
    "15m": timedelta(minutes=15),
}


def _naive_utc(dt: datetime) -> datetime:
    if dt.tzinfo is not None:
        return dt.astimezone(UTC).replace(tzinfo=None)
    return dt


def _recent_ticks(session: Session, symbol_code: str, since: datetime) -> pd.DataFrame:
    rows = session.exec(
        select(Tick)
        .where(Tick.symbol_code == symbol_code, Tick.ts >= since)
        .order_by(Tick.ts)
    ).all()
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(
        [{"ts": r.ts, "price": r.price, "volume": r.volume} for r in rows if r.price]
    )
    if df.empty:
        return df
    df = df.set_index("ts")
    df.index = pd.to_datetime(df.index)
    if df.index.tz is not None:
        df.index = df.index.tz_convert("UTC").tz_localize(None)
    return df


def build_bars(
    session: Session,
    timeframes: tuple[str, ...] = ("1m", "5m"),
    lookback_minutes: int = 180,
    now: datetime | None = None,
) -> int:
    now = _naive_utc(now) if now else datetime.now(UTC).replace(tzinfo=None)
    since = now - timedelta(minutes=lookback_minutes)

    codes = {
        c for c in session.exec(select(Tick.symbol_code).where(Tick.ts >= since).distinct()).all()
    }
    total = 0
    now_ts = pd.Timestamp(now)
    for code in sorted(codes):
        ticks = _recent_ticks(session, code, since)
        if ticks.empty:
            continue
        vol_delta = ticks["volume"].diff().fillna(0.0).clip(lower=0.0)

        for tf in timeframes:
            rule = TIMEFRAME_TO_PANDAS[tf]
            g = ticks["price"].resample(rule, label="left", closed="left")
            bars = g.ohlc()
            bars.columns = ["open", "high", "low", "close"]
            bars["volume"] = vol_delta.resample(rule, label="left", closed="left").sum()
            bars = bars.dropna(subset=["open"])
            if bars.empty:
                continue
            completed = bars[bars.index + TF_DELTA[tf] <= now_ts]
            if not completed.empty:
                total += upsert_bars(session, code, tf, completed, source="rss")
    return total
