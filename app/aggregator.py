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
    """since 以降の tick。累計出来高の階差を取るため、since 直前の1件も prev=True で付ける。"""
    rows = session.exec(
        select(Tick)
        .where(Tick.symbol_code == symbol_code, Tick.ts >= since)
        .order_by(Tick.ts)
    ).all()
    if not rows:
        return pd.DataFrame()
    prev = session.exec(
        select(Tick)
        .where(Tick.symbol_code == symbol_code, Tick.ts < since, Tick.price > 0)
        .order_by(Tick.ts.desc())
        .limit(1)
    ).first()
    recs = [{"ts": r.ts, "price": r.price, "volume": r.volume, "prev": False} for r in rows if r.price]
    if prev is not None:
        recs.insert(0, {"ts": prev.ts, "price": prev.price, "volume": prev.volume, "prev": True})
    df = pd.DataFrame(recs)
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
        vol = ticks["volume"].fillna(0.0)
        diff = vol.diff()
        # 累計出来高が減った＝日付が変わってリセット。その tick までの出来高は新しい累計値そのもの
        diff = diff.where(diff.isna() | (diff >= 0), vol)
        if (vol > 0).any():
            # 約定があった tick だけ。直前 tick が無い先頭（diff が NaN）は約定扱いで残す
            traded = diff.isna() | (diff > 0)
        else:
            traded = pd.Series(True, index=ticks.index)  # 出来高の情報が無い: 全 tick で作る
        keep = traded & ~ticks["prev"]
        vol_delta = diff.fillna(0.0).clip(lower=0.0)[keep]
        ticks = ticks[keep]
        if ticks.empty:
            continue

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
