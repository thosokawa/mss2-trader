"""発注インターフェース。

- PaperBroker: 約定をシミュレーション（P3 のペーパートレードで使用。同期・即約定）
- 実発注（P4）: RssStockOrder は Excel の式評価 + セルの状態確定を待つ必要があり、
  本質的に非同期（backend が Order を作る → bridge が拾って発注 → 結果を報告、の
  リレー）。そのため PaperBroker のような同期 place() では表現できず、
  app/engine/orders.py（キュー管理）+ app/web/routes.py の
  /api/orders/pending, /api/orders/{id}/report + bridge/bridge.py が本体。
  ここでの RssBroker は API 一覧性のためのプレースホルダとして残す。
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
    """未使用（プレースホルダ）。実発注は非同期のため place() では表現できない。
    実体は app/engine/orders.py + /api/orders/* + bridge/bridge.py を参照。"""

    def place(self, symbol: str, signal: Signal, ref_price: float) -> OrderResult:
        raise NotImplementedError("place() は使わない。app.engine.orders.queue_order() を使うこと。")
