"""過去足を yfinance から取得して DB に保存する共通ロジック。

scripts/fetch_history.py（CLI）と Web UI（/data の「過去データを取得」フォーム）の
両方から使う。日本株は ".T" を付けて照会する（例 7203 -> 7203.T）。

yfinance の分足制約: 1m は直近7日、5m/15m は直近60日程度まで。
恒久的な分足ヒストリは P1 以降 RSS ブリッジで自前蓄積する前提。
"""
from __future__ import annotations

import pandas as pd
import yfinance as yf
from sqlmodel import Session

from app.bars import upsert_bars
from app.models import Symbol

INTERVAL_MAP = {"1m": "1m", "5m": "5m", "15m": "15m", "1d": "1d"}


def fetch_one(code: str, interval: str, period: str) -> pd.DataFrame:
    """yfinance から1銘柄ぶんの足を取得する（DBには保存しない）。"""
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


def fetch_and_store(session: Session, code: str, interval: str, period: str) -> dict:
    """1銘柄ぶん取得して DB に保存する。CLI/Web 共通の結果 dict を返す。

    {"code", "ok", "n", "start", "end"} または失敗時 {"code", "ok"=False, "n"=0, "message"}
    """
    try:
        df = fetch_one(code, interval, period)
    except Exception as e:  # noqa: BLE001 - yfinance/ネットワーク由来の例外を画面に出すため
        return {"code": code, "ok": False, "n": 0, "message": str(e)}
    if df.empty:
        msg = "データ取得できず（コード/期間/intervalを確認）"
        return {"code": code, "ok": False, "n": 0, "message": msg}
    if not session.get(Symbol, code):
        session.add(Symbol(code=code))
        session.commit()
    n = upsert_bars(session, code, interval, df, source="yfinance")
    return {"code": code, "ok": True, "n": n, "start": df.index[0], "end": df.index[-1]}
