"""過去足を yfinance から取得して DB に保存する。

  python scripts/fetch_history.py 7203 6501 --interval 5m --period 60d
  python scripts/fetch_history.py 7203 --interval 1d --period 2y

yfinance の分足制約: 1m は直近7日、5m/15m は直近60日程度まで。
日本株は ".T" を付けて照会する（例 7203 -> 7203.T）。
恒久的な分足ヒストリは P1 以降 RSS ブリッジで自前蓄積する前提。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402
import yfinance as yf  # noqa: E402
from sqlmodel import Session  # noqa: E402

from app.bars import upsert_bars  # noqa: E402
from app.db import engine, init_db  # noqa: E402
from app.models import Symbol  # noqa: E402

INTERVAL_MAP = {"1m": "1m", "5m": "5m", "15m": "15m", "1d": "1d"}


def fetch_one(code: str, interval: str, period: str) -> pd.DataFrame:
    ticker = f"{code}.T"
    raw = yf.download(
        ticker, period=period, interval=INTERVAL_MAP[interval], auto_adjust=False, progress=False
    )
    if raw.empty:
        return raw
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.get_level_values(0)
    df = raw.rename(
        columns={"Open": "open", "High": "high", "Low": "low", "Close": "close", "Volume": "volume"}
    )[["open", "high", "low", "close", "volume"]]
    df.index = pd.to_datetime(df.index, utc=True).tz_convert(None)
    return df.dropna(subset=["open"])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("codes", nargs="+", help="証券コード 例: 7203 6501")
    ap.add_argument("--interval", default="5m", choices=list(INTERVAL_MAP))
    ap.add_argument("--period", default="60d")
    args = ap.parse_args()

    init_db()
    with Session(engine) as s:
        for code in args.codes:
            df = fetch_one(code, args.interval, args.period)
            if df.empty:
                print(f"{code}: データ取得できず（コード/期間/interval を確認）")
                continue
            if not s.get(Symbol, code):
                s.add(Symbol(code=code))
                s.commit()
            n = upsert_bars(s, code, args.interval, df, source="yfinance")
            print(f"{code}: {n} 本 保存（{df.index[0]} 〜 {df.index[-1]}）")


if __name__ == "__main__":
    main()
