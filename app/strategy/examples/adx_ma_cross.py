"""ADX でトレンドの強さを確認したうえで SMA クロスに従う。

SMAクロス（sma_cross.py）はレンジ相場でクロスが頻発して「ダマシ」が多くなりがち。
ADX（トレンドの強さ、方向は問わない）が閾値以上のときだけエントリーを許可することで
それを抑える —素のクロス戦略の改良版という位置づけ。
"""
from __future__ import annotations

from app.strategy.base import Context, Signal, Strategy
from app.strategy.indicators import adx, crossed_down, crossed_up, sma


class AdxMaCross(Strategy):
    timeframe = "5m"
    description = "ADXでトレンドが強いと確認できたときだけSMAゴールデンクロスに従う（レンジ相場のダマシ回避）"
    default_params = {
        "fast": 5,
        "slow": 20,
        "adx_period": 14,
        "adx_min": 20.0,
        "qty": 100,
    }
    param_meta = {
        "fast": {"label": "短期SMA期間"},
        "slow": {"label": "長期SMA期間"},
        "adx_period": {"label": "ADX期間"},
        "adx_min": {"label": "ADXの下限", "help": "この値以上のときだけトレンドが強いと判定してエントリー"},
        "qty": {"label": "株数", "help": "1回のエントリーで売買する株数"},
    }

    def on_bar(self, ctx: Context) -> Signal | None:
        p = self.params
        c = ctx.close
        fast_n, slow_n, adx_n = int(p["fast"]), int(p["slow"]), int(p["adx_period"])
        if len(c) < max(slow_n, adx_n) + 2:
            return None

        fast = sma(c, fast_n)
        slow = sma(c, slow_n)
        adx_line, _, _ = adx(ctx.bars["high"], ctx.bars["low"], c, adx_n)
        adx_now = float(adx_line.iloc[-1])

        if ctx.position.is_flat:
            if adx_now >= float(p["adx_min"]) and crossed_up(fast, slow):
                return Signal("BUY", int(p["qty"]), reason=f"GC（ADX{adx_now:.0f} 強）")
            return None

        if ctx.position.is_long and crossed_down(fast, slow):
            return Signal("EXIT", reason=f"DC（ADX{adx_now:.0f}）")
        return None
