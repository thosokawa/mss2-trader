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
    """建玉。qty > 0 が買い持ち（ロング）、qty < 0 が売り持ち（ショート＝信用の売建）。"""

    qty: int = 0
    avg_price: float = 0.0

    @property
    def is_flat(self) -> bool:
        return self.qty == 0

    @property
    def is_long(self) -> bool:
        return self.qty > 0

    @property
    def is_short(self) -> bool:
        return self.qty < 0

    @property
    def direction(self) -> int:
        """+1=ロング / -1=ショート / 0=ノーポジ。損益は (価格差) × |qty| × direction。"""
        return (self.qty > 0) - (self.qty < 0)


@dataclass
class Signal:
    # "BUY"   : 新規買い（ロング）
    # "SHORT" : 新規売り（ショート＝信用の売建。allow_short のときだけ出す）
    # "EXIT"  : 今の建玉を手仕舞う（ロングなら売り、ショートなら買い戻し）
    # "SELL"  : 旧来の書き方。ロングの手仕舞いとして扱う（EXIT と同じ）
    side: str
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

    @property
    def allow_short(self) -> bool:
        """全戦略共通パラメータ allow_short（空売りする）。on_bar で SHORT を出してよいか。"""
        v = self.params.get("allow_short", False)
        if isinstance(v, str):
            return v.strip().lower() in ("true", "1", "yes", "on")
        return bool(v)

    def on_bar(self, ctx: Context) -> Signal | None:  # pragma: no cover - 抽象
        raise NotImplementedError

    # ロジック作者向けの説明文（UI に表示）
    description: str = ""

    # UI がパラメータ入力欄を組み立てるためのヒント（無くても動く。省略したキーは
    # 項目名がそのまま表示される）。例:
    #   {"fast": {"label": "短期期間", "help": "短期移動平均の本数"},
    #    "ma_type": {"label": "種類", "choices": ["ema", "sma"]}}
    param_meta: dict[str, dict] = {}
