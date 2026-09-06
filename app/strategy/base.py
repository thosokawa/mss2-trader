"""売買ロジックの共通インターフェース。

live.py（本番）と backtest.py（検証）は同じ Strategy.on_bar() を呼ぶ。
これにより「バックテストで良かったロジックが本番で別物になる」事故を防ぐ。

ロジック作者が書くのは on_bar() だけ。1本の足が確定するたびに呼ばれ、
売買したいときだけ Signal を返す（何もしないときは None）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import pandas as pd


@dataclass
class Position:
    qty: int = 0
    avg_price: float = 0.0

    @property
    def is_flat(self) -> bool:
        return self.qty == 0

    @property
    def is_long(self) -> bool:
        return self.qty > 0


@dataclass
class Signal:
    side: str  # "BUY" | "SELL" | "EXIT"
    qty: int | None = None  # None なら戦略パラメータ / リスク層が決める
    reason: str = ""
    order_type: str = "MKT"  # "MKT" | "LMT"
    limit_price: float | None = None


@dataclass
class Context:
    symbol: str
    now: datetime
    bars: pd.DataFrame  # index=ts, 列 open/high/low/close/volume。最終行が確定した最新足。
    position: Position
    params: dict = field(default_factory=dict)

    @property
    def price(self) -> float:
        return float(self.bars["close"].iloc[-1])

    @property
    def close(self) -> pd.Series:
        return self.bars["close"]


class Strategy:
    timeframe: str = "5m"
    default_params: dict = {}

    def __init__(self, params: dict | None = None):
        self.params = {**self.default_params, **(params or {})}

    def on_bar(self, ctx: Context) -> Signal | None:  # pragma: no cover - 抽象
        raise NotImplementedError

    # ロジック作者向けの説明文（UI に表示）
    description: str = ""
