"""自動売買の安全機構。P4 で本格実装。P0 では骨子と ARMED トグルの置き場だけ用意。

思想:
- 既定は必ず「発注しない」。複数のガードを AND で通過したときだけ発注を許可する。
- ARMED はプロセス内メモリ + DB。再起動時は必ず False に戻す。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time

from app.config import TradingCfg


@dataclass
class RiskState:
    armed: bool = False  # UI トグル。プロセス再起動で False。
    day_realized_pnl: float = 0.0
    halted_reason: str = ""


class RiskEngine:
    def __init__(self, cfg: TradingCfg, state: RiskState | None = None):
        self.cfg = cfg
        self.state = state or RiskState()

    def arm(self) -> None:
        self.state.armed = True
        self.state.halted_reason = ""

    def disarm(self, reason: str = "manual") -> None:
        self.state.armed = False
        self.state.halted_reason = reason

    def in_session(self, now: datetime) -> bool:
        t = now.time()
        for w in self.cfg.session_windows:
            a, b = w.split("-")
            start = time.fromisoformat(a)
            end = time.fromisoformat(b)
            if start <= t <= end:
                return True
        return False

    def check(self, *, now: datetime, side: str, qty: int, price: float, mode: str) -> tuple[bool, str]:
        """(発注してよいか, 理由) を返す。"""
        if mode != "live":
            return False, f"mode={mode}（発注対象外）"
        if not self.cfg.enabled:
            return False, "config.trading.enabled=false"
        if not self.state.armed:
            return False, "DISARMED"
        if self.state.halted_reason:
            return False, f"halted: {self.state.halted_reason}"
        if not self.in_session(now):
            return False, "取引時間外"
        if qty <= 0 or qty > self.cfg.max_qty_per_order:
            return False, f"数量NG qty={qty} 上限={self.cfg.max_qty_per_order}"
        if qty * price > self.cfg.max_notional_per_order:
            return False, f"金額NG {qty * price:.0f} 上限={self.cfg.max_notional_per_order}"
        if self.state.day_realized_pnl <= -abs(self.cfg.daily_loss_limit):
            return False, "日次損失リミット到達"
        return True, "ok"
