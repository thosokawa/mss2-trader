"""自動売買の安全機構（P4）。

思想:
- 既定は必ず「発注しない」。複数のガードを AND で通過したときだけ発注を許可する。
- ARMED はプロセス内メモリのみ。プロセス再起動で必ず False に戻る（DB には保存しない）。
- mode="live" の戦略のシグナルであっても、config.trading.enabled と ARMED の
  両方が true でない限り発注しない（config はコード外から明示的に変更する必要が
  あり、ARMED は画面のボタンだが再起動でリセットされる — 二重の安全弁）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time

from app.config import TradingCfg, get_config


@dataclass
class RiskState:
    armed: bool = False  # UI トグル。プロセス再起動で False。
    day: date = field(default_factory=date.today)
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

    def _roll_day(self, today: date) -> None:
        if today != self.state.day:
            self.state.day = today
            self.state.day_realized_pnl = 0.0

    def record_fill_pnl(self, pnl: float, *, today: date | None = None) -> None:
        """約定による実現損益を日次集計に足す。損失リミット判定に使う。"""
        self._roll_day(today or date.today())
        self.state.day_realized_pnl += pnl
        if self.state.day_realized_pnl <= -abs(self.cfg.daily_loss_limit):
            self.disarm("日次損失リミット到達")

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
        self._roll_day(now.date())
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


_ENGINE: RiskEngine | None = None


def get_risk_engine() -> RiskEngine:
    """プロセス内シングルトン。ARMED 状態はここに保持される（再起動で消える）。"""
    global _ENGINE
    if _ENGINE is None:
        _ENGINE = RiskEngine(get_config().trading)
    return _ENGINE
