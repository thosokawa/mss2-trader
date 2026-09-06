"""ライブのシグナル生成ループ（P2 で本格実装）。

流れ（予定）:
  bridge から tick 受信 -> bars.ticks_to_bars で足に集約 -> 足が「確定」したら
  対象の enabled な Strategy を on_bar 実行 -> Signal が出たら:
    - Signal を DB 保存（idempotency_key = strategy:symbol:bar_ts で重複防止）
    - notify.send_slack で通知
    - mode=="live" かつ RiskEngine.check が通れば Broker.place で発注

P0 では単発評価のユーティリティのみ提供（バックテストと同じ on_bar を使う確認用）。
"""
from __future__ import annotations

from datetime import datetime

import pandas as pd

from app.strategy.base import Context, Position, Strategy


def evaluate_latest(strategy: Strategy, bars: pd.DataFrame, symbol: str, position: Position | None = None):
    """確定済み足の列に対して最新1本ぶんの判定を返す。live ループの中核部分の切り出し。"""
    if bars.empty:
        return None
    now = pd.Timestamp(bars.index[-1]).to_pydatetime()
    ctx = Context(symbol=symbol, now=now, bars=bars, position=position or Position(), params=strategy.params)
    return strategy.on_bar(ctx)


def idempotency_key(strategy_name: str, symbol: str, bar_ts: datetime) -> str:
    return f"{strategy_name}:{symbol}:{bar_ts.isoformat()}"
