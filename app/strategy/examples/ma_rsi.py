"""移動平均と終値(実線)の関係 + RSI でエントリーを出す戦略。

パラメータで挙動を変えられる。まずは「エントリーポイントを出す」ことが目的で、
エグジットは単純（MA 下抜け or RSI 過熱）。損切り/利確は engine 拡張後に足す。

エントリー(BUY) … 建玉が無く、次をすべて満たす確定足で
  1. price_vs_ma  : "cross_up" = 終値が MA を上抜け / "above" = 終値 > MA
  2. ma_slope_min : MA の傾き（slope_lookback 本前からの変化率, %）が下限以上
  3. rsi_min <= RSI(rsi_period) <= rsi_max
  4. dev_max_pct  : MA からの上方乖離が上限以下（飛びつき防止, 0 で無効）

エグジット(EXIT) … 建玉があり、終値が MA を下抜け、または RSI >= rsi_exit
"""
from __future__ import annotations

from app.strategy.base import Context, Signal, Strategy
from app.strategy.indicators import (
    crossed_up,
    deviation_pct,
    ema,
    rsi,
    slope_pct,
    sma,
)


class MaRsi(Strategy):
    timeframe = "5m"
    description = "移動平均と終値の関係（上抜け/上方）+ 傾き + RSI帯でエントリー、MA下抜けかRSI過熱で手仕舞い"
    default_params = {
        "ma_type": "ema",          # "ema" | "sma"
        "ma_period": 25,
        "price_vs_ma": "cross_up",  # "cross_up" | "above"
        "slope_lookback": 5,
        "ma_slope_min": 0.0,        # %。MA が上向きのときだけ買うなら 0 以上
        "rsi_period": 14,
        "rsi_min": 45.0,
        "rsi_max": 70.0,
        "rsi_exit": 78.0,
        "dev_max_pct": 0.0,        # MA からの上方乖離の上限(%)。0 で無効
        "qty": 100,
    }

    def on_bar(self, ctx: Context) -> Signal | None:
        p = self.params
        c = ctx.close
        ma_n = int(p["ma_period"])
        rsi_n = int(p["rsi_period"])
        look = int(p["slope_lookback"])
        if len(c) < max(ma_n, rsi_n) + look + 2:
            return None

        ma = ema(c, ma_n) if p["ma_type"] == "ema" else sma(c, ma_n)
        r = rsi(c, rsi_n)
        price = float(c.iloc[-1])
        ma_now = float(ma.iloc[-1])
        rsi_now = float(r.iloc[-1])
        slope = float(slope_pct(ma, look).iloc[-1])
        dev = float(deviation_pct(c, ma).iloc[-1])

        if ctx.position.is_flat:
            if p["price_vs_ma"] == "cross_up":
                cond_price = crossed_up(c, ma)
            else:
                cond_price = price > ma_now
            cond_slope = slope >= float(p["ma_slope_min"])
            cond_rsi = float(p["rsi_min"]) <= rsi_now <= float(p["rsi_max"])
            dev_max = float(p["dev_max_pct"])
            cond_dev = dev_max <= 0 or dev <= dev_max
            if cond_price and cond_slope and cond_rsi and cond_dev:
                return Signal(
                    "BUY",
                    int(p["qty"]),
                    reason=(
                        f"{p['ma_type'].upper()}{ma_n} "
                        f"{'上抜け' if p['price_vs_ma'] == 'cross_up' else '上方'} "
                        f"価格{price:.1f}/MA{ma_now:.1f} 傾き{slope:+.2f}% RSI{rsi_now:.0f} 乖離{dev:+.2f}%"
                    ),
                )
            return None

        if ctx.position.is_long and (price < ma_now or rsi_now >= float(p["rsi_exit"])):
            why = "MA下抜け" if price < ma_now else f"RSI{rsi_now:.0f}過熱"
            return Signal("EXIT", reason=f"{why} 価格{price:.1f}")
        return None
