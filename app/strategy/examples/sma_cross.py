"""サンプル戦略: 単純移動平均のゴールデンクロス / デッドクロス。

これは「自分でロジックを組む」ときの雛形。on_bar だけ書けばよい、という例。
実運用向けではなく、パイプライン（取得→バックテスト→シグナル→通知）の疎通確認用。
"""
from __future__ import annotations

from app.strategy.base import Context, Signal, Strategy


class SmaCross(Strategy):
    timeframe = "5m"
    description = "短期SMAが長期SMAを上抜けで買い、下抜けで手仕舞い"
    default_params = {"fast": 5, "slow": 20, "qty": 100}

    def on_bar(self, ctx: Context) -> Signal | None:
        fast_n = int(self.params["fast"])
        slow_n = int(self.params["slow"])
        c = ctx.close
        if len(c) < slow_n + 2:
            return None

        fast = c.rolling(fast_n).mean()
        slow = c.rolling(slow_n).mean()
        prev = fast.iloc[-2] - slow.iloc[-2]
        cur = fast.iloc[-1] - slow.iloc[-1]

        f, sl = fast.iloc[-1], slow.iloc[-1]
        if prev <= 0 < cur and ctx.position.is_flat:
            return Signal("BUY", int(self.params["qty"]),
                          reason=f"GC fast{fast_n}={f:.1f} > slow{slow_n}={sl:.1f}")
        if prev >= 0 > cur and ctx.position.is_long:
            return Signal("EXIT", reason=f"DC fast{fast_n}={f:.1f} < slow{slow_n}={sl:.1f}")
        return None
