"""ボリンジャーバンドの逆張り（レンジ相場向け）。

今までの3例（SMAクロス・移動平均+RSI・トレンド×RSI出戻り）はすべてトレンドフォロー系。
これは逆張り＝下限を割れたあとの反発を狙う、性質の違うパターン。
トレンドが強い相場では機能しにくいので、ADX 等でレンジ相場を確認してから使うのが定石。
"""
from __future__ import annotations

from app.strategy.base import Context, Signal, Strategy
from app.strategy.indicators import bollinger_bands, crossed_up


class BollingerReversion(Strategy):
    timeframe = "5m"
    description = "終値が下限バンドを割ってから再び上抜けで買い（逆張り）。中心線か上限で手仕舞い"
    default_params = {
        "period": 20,
        "num_std": 2.0,
        "exit_at": "mid",  # "mid"（中心線）| "upper"（上限バンド）
        "qty": 100,
    }

    def on_bar(self, ctx: Context) -> Signal | None:
        p = self.params
        c = ctx.close
        n = int(p["period"])
        if len(c) < n + 2:
            return None

        mid, upper, lower = bollinger_bands(c, n, float(p["num_std"]))
        price = float(c.iloc[-1])

        if ctx.position.is_flat:
            if crossed_up(c, lower):
                return Signal(
                    "BUY",
                    int(p["qty"]),
                    reason=f"下限反発 終値{price:.1f}>下限{lower.iloc[-1]:.1f}",
                )
            return None

        if ctx.position.is_long:
            target = upper if p["exit_at"] == "upper" else mid
            label = "上限" if p["exit_at"] == "upper" else "中心線"
            if price >= float(target.iloc[-1]):
                return Signal("EXIT", reason=f"{label}到達 終値{price:.1f}")
        return None
