"""大引けをまたぐか（オーバーナイト保有）の判定。バックテストと live 両方から使う共通ロジック。

戦略パラメータ `hold_overnight`（`registry.builtin_params()` が全戦略に共通で持たせる、
既定 True＝従来どおり持ち越す）を False にすると、日中足（1m/5m/15m）では:
  - 大引け前の「最後に動ける足」で建玉を成行手仕舞いする（on_bar の判断より優先。
    損切り/利確に達していればそちらを優先）
  - その足では新規買いをしない（すぐ手仕舞いになるだけなので）
日足（1d）では意味が無いので常に無視する。

「最後に動ける足」= 次の足が EOD_CUTOFF（JST）までに確定しない足。
足の確定後に成行を出すので、15:25〜15:30 のクロージング・オークションに間に合うよう
余裕を持たせて 15:20 にしている。例: 5m→15:15〜15:20 の足、15m→15:00〜15:15 の足。
バックテストでは加えて「次の足が別の日」の足も大引けとみなす（2024/11 以前の 15:00 引けの
データや、半日立会・データ欠けに対応するため）。

足の ts は「足の開始時刻・naive UTC」（DB の規約）。
"""
from __future__ import annotations

from datetime import datetime, time, timedelta

JST_OFFSET = timedelta(hours=9)
EOD_CUTOFF = time(15, 20)

INTRADAY_TF = {
    "1m": timedelta(minutes=1),
    "5m": timedelta(minutes=5),
    "15m": timedelta(minutes=15),
}


def flatten_at_close(params: dict, timeframe: str) -> bool:
    """この戦略・足で大引け手仕舞いを行うか（hold_overnight=False かつ日中足）。"""
    if timeframe not in INTRADAY_TF:
        return False
    v = params.get("hold_overnight", True)
    if isinstance(v, str):
        v = v.strip().lower() not in ("false", "0", "no", "off", "")
    return not bool(v)


def _jst(ts: datetime) -> datetime:
    return ts + JST_OFFSET


def is_last_bar_of_day(ts: datetime, timeframe: str, next_ts: datetime | None = None) -> bool:
    """ts（足の開始・naive UTC）の足が、その日の大引け前に動ける最後の足か。"""
    tf = INTRADAY_TF.get(timeframe)
    if tf is None:
        return False
    start = _jst(ts)
    cutoff = datetime.combine(start.date(), EOD_CUTOFF)
    if start + tf + tf > cutoff:
        return True
    return next_ts is not None and _jst(next_ts).date() != start.date()


def is_new_day(prev_ts: datetime | None, ts: datetime) -> bool:
    """前の足と日付（JST）が変わったか。live で前日からの持ち越し建玉を検出するのに使う。"""
    return prev_ts is not None and _jst(prev_ts).date() != _jst(ts).date()
