"""トレンド方向 × RSI の 40/60 ライン出戻りでエントリー通知を出す戦略。

- 短期MA > 中期MA（上昇トレンド）で、RSI が一旦 rsi_buy_level（既定40）以下に
  なったあと再び上抜けた足 → BUY
- 短期MA < 中期MA（下降トレンド）で、RSI が一旦 rsi_sell_level（既定60）以上に
  なったあと再び下抜けた足 → SELL

「一旦下（上）に行ってから戻る」は 40（60）ラインの上抜け（下抜け）そのもので判定する
（上抜けるには直前が 40 以下である必要があるため）。

※ ポジションは見ない純粋なアラート戦略。mode=notify 向け。
   売り（空売り）は現エンジン未対応なので paper/backtest では BUY 側しか約定しない。
"""
from __future__ import annotations

from app.strategy.base import Context, Signal, Strategy
from app.strategy.indicators import ema, rsi, sma


class TrendRsiReclaim(Strategy):
    timeframe = "5m"
    description = "短期MA>中期MAでRSIが40を回復→買い通知 / 短期MA<中期MAでRSIが60を割れ→売り通知"
    default_params = {
        "ma_type": "sma",       # "sma" | "ema"
        "fast_period": 10,
        "mid_period": 40,
        "rsi_period": 14,
        "rsi_buy_level": 40.0,
        "rsi_sell_level": 60.0,
        "qty": 100,
    }

    def on_bar(self, ctx: Context) -> Signal | None:
        p = self.params
        c = ctx.close
        fast_n = int(p["fast_period"])
        mid_n = int(p["mid_period"])
        rsi_n = int(p["rsi_period"])
        if len(c) < max(mid_n, rsi_n) + 2:
            return None

        ma = ema if p["ma_type"] == "ema" else sma
        fast = ma(c, fast_n)
        mid = ma(c, mid_n)
        r = rsi(c, rsi_n)
        r_prev = float(r.iloc[-2])
        r_now = float(r.iloc[-1])
        up_trend = fast.iloc[-1] > mid.iloc[-1]
        down_trend = fast.iloc[-1] < mid.iloc[-1]

        buy_lv = float(p["rsi_buy_level"])
        sell_lv = float(p["rsi_sell_level"])
        tag = f"{p['ma_type'].upper()}{fast_n}/{mid_n}"

        if up_trend and r_prev <= buy_lv < r_now:
            return Signal(
                "BUY",
                int(p["qty"]),
                reason=f"上昇({tag}) RSI {r_prev:.0f}→{r_now:.0f} が{buy_lv:.0f}回復",
            )
        if down_trend and r_prev >= sell_lv > r_now:
            return Signal(
                "SELL",
                int(p["qty"]),
                reason=f"下降({tag}) RSI {r_prev:.0f}→{r_now:.0f} が{sell_lv:.0f}割れ",
            )
        return None
