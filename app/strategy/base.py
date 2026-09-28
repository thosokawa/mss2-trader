"""売買ロジックの共通インターフェース。

live.py（本番）と backtest.py（検証）は同じ Strategy.decide()（= on_bar() + 売買方向の絞り込み）を呼ぶ。
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
    # "SHORT" : 新規売り（ショート＝信用の売建。self.allow_short のときだけ出す）
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
    def direction(self) -> str:
        """全戦略共通パラメータ direction（売買方向）: "long"=買いのみ / "short"=売りのみ / "both"=両方。

        旧パラメータ allow_short（空売りする、bool）だけが保存されている戦略は
        true → "both"、それ以外 → "long" として読む（既定は "long"＝従来どおり買いのみ）。
        """
        v = str(self.params.get("direction") or "").strip().lower()
        if v in ("long", "short", "both"):
            return v
        legacy = self.params.get("allow_short", False)
        if isinstance(legacy, str):
            legacy = legacy.strip().lower() in ("true", "1", "yes", "on")
        return "both" if legacy else "long"

    @property
    def allow_short(self) -> bool:
        """on_bar で SHORT（売建）を出してよいか（direction が short / both）。"""
        return self.direction in ("short", "both")

    @property
    def allow_long(self) -> bool:
        """BUY（新規買い）を出してよいか（direction が long / both）。"""
        return self.direction in ("long", "both")

    def decide(self, ctx: Context) -> Signal | None:
        """エンジン（backtest / live）が呼ぶ入口。on_bar の結果から売買方向に合わない
        新規建てを捨てる（手仕舞いは常に通す）。各戦略は BUY/SHORT を気にせず書いてよい。"""
        sig = self.on_bar(ctx)
        if sig is None:
            return None
        if sig.side == "BUY" and not self.allow_long:
            return None
        if sig.side == "SHORT" and not self.allow_short:
            return None
        return sig

    def on_bar(self, ctx: Context) -> Signal | None:  # pragma: no cover - 抽象
        raise NotImplementedError

    # ロジック作者向けの説明文（UI に表示）
    description: str = ""

    # UI がパラメータ入力欄を組み立てるためのヒント（無くても動く。省略したキーは
    # 項目名がそのまま表示される）。例:
    #   {"fast": {"label": "短期期間", "help": "短期移動平均の本数"},
    #    "ma_type": {"label": "種類", "choices": ["ema", "sma"]}}
    param_meta: dict[str, dict] = {}
