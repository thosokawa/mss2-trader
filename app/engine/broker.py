"""発注インターフェース。engine はこの抽象だけに依存する。

- PaperBroker: 約定をシミュレーション（P3 のペーパートレードで使用）
- RssBroker  : Windows の bridge 経由で MarketSpeed II RSS に発注（P4 で実装）

P0 では未使用。IF を先に固定しておくことで engine 側のコードが後から変わらない。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.strategy.base import Signal


@dataclass
class OrderResult:
    ok: bool
    broker_order_id: str = ""
    filled_qty: int = 0
    avg_price: float = 0.0
    error: str = ""


class Broker:
    def place(self, symbol: str, signal: Signal, ref_price: float) -> OrderResult:  # pragma: no cover
        raise NotImplementedError


@dataclass
class PaperBroker(Broker):
    """参照価格で即時フル約定したものとして扱う。手数料/スリッページは slippage_bps で概算。"""

    slippage_bps: float = 0.0
    _seq: int = 0
    fills: list[dict] = field(default_factory=list)

    def place(self, symbol: str, signal: Signal, ref_price: float) -> OrderResult:
        self._seq += 1
        slip = ref_price * self.slippage_bps / 10_000
        px = ref_price + slip if signal.side == "BUY" else ref_price - slip
        qty = int(signal.qty or 0)
        self.fills.append(
            {"ts": datetime.now(UTC), "symbol": symbol, "side": signal.side, "qty": qty, "price": px}
        )
        return OrderResult(ok=True, broker_order_id=f"paper-{self._seq}", filled_qty=qty, avg_price=px)


class RssBroker(Broker):
    """bridge にHTTPで発注指令を出し、RssOrder の実行結果を受け取る（P4 で実装）。"""

    def __init__(self, command_url: str):
        self.command_url = command_url

    def place(self, symbol: str, signal: Signal, ref_price: float) -> OrderResult:
        raise NotImplementedError("P4 で実装。bridge 側の RssOrder ラッパと合わせて作る。")
