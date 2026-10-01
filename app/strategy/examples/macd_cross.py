"""MACD のゴールデン/デッドクロス。モメンタム系オシレーターの基本形。

gap_filter（ギャップの向きにだけ建てる）を ON にすると、トレンド×RSI出戻りと同じく、当日の始値が
前日終値より上の日は買いだけ、下の日は売りだけ建てる（Strategy.gap_allows。手仕舞いは制限しない）。
"""
from __future__ import annotations

from app.strategy.base import Context, Signal, Strategy
from app.strategy.indicators import crossed_down, crossed_up, macd


class MacdCross(Strategy):
    timeframe = "5m"
    description = ("MACD線がシグナル線を上抜けで買い、下抜けで手仕舞い（ゼロライン上のみに絞ることも可）。"
                   "空売りありなら下抜けで売建（ゼロライン下のみ）、上抜けで買戻し")
    default_params = {
        "fast": 12,
        "slow": 26,
        "signal": 9,
        "require_above_zero": False,  # true にすると MACD>0（中期的に上昇基調）のときだけ買う
        "gap_filter": False,
        "qty": 100,
    }
    param_meta = {
        "fast": {"label": "MACD短期期間"},
        "slow": {"label": "MACD長期期間"},
        "signal": {"label": "シグナル期間"},
        "require_above_zero": {
            "label": "ゼロライン上のみ買う",
            "help": "オンにするとMACDがプラスのときだけ買う（空売りはマイナスのときだけ）",
        },
        "gap_filter": {
            "label": "ギャップの向きにだけ建てる", "type": "bool",
            "help": "ON: 当日の始値が前日終値より上の日は買いだけ、下の日は売りだけ建てる"
                    "（手仕舞いは制限しない）",
        },
        "qty": {"label": "株数", "help": "1回のエントリーで売買する株数"},
    }

    def on_bar(self, ctx: Context) -> Signal | None:
        p = self.params
        c = ctx.close
        fast_n, slow_n, sig_n = int(p["fast"]), int(p["slow"]), int(p["signal"])
        if len(c) < slow_n + sig_n + 2:
            return None

        line, sig, _ = macd(c, fast_n, slow_n, sig_n)

        if ctx.position.is_flat:
            can_buy, can_short = self.gap_allows(ctx)
            if can_buy and crossed_up(line, sig) and (not p["require_above_zero"] or line.iloc[-1] > 0):
                return Signal(
                    "BUY",
                    int(p["qty"]),
                    reason=f"MACD上抜け {line.iloc[-1]:.2f}>{sig.iloc[-1]:.2f}",
                )
            if (can_short and self.allow_short and crossed_down(line, sig)
                    and (not p["require_above_zero"] or line.iloc[-1] < 0)):
                return Signal(
                    "SHORT",
                    int(p["qty"]),
                    reason=f"MACD下抜け {line.iloc[-1]:.2f}<{sig.iloc[-1]:.2f}",
                )
            return None

        if ctx.position.is_long and crossed_down(line, sig):
            return Signal("EXIT", reason=f"MACD下抜け {line.iloc[-1]:.2f}<{sig.iloc[-1]:.2f}")
        if ctx.position.is_short and crossed_up(line, sig):
            return Signal("EXIT", reason=f"MACD上抜け {line.iloc[-1]:.2f}>{sig.iloc[-1]:.2f}")
        return None
