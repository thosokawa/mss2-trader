"""トレンド方向 × RSI の 40/60 ライン出戻りでエントリー通知を出す戦略。

- 短期MA > 中期MA（上昇トレンド）で、RSI が一旦 rsi_buy_level（既定40）以下に
  なったあと再び上抜けた足 → BUY
- 短期MA < 中期MA（下降トレンド）で、RSI が一旦 rsi_sell_level（既定60）以上に
  なったあと再び下抜けた足 → SELL

「一旦下（上）に行ってから戻る」は 40（60）ラインの上抜け（下抜け）そのもので判定する
（上抜けるには直前が 40 以下である必要があるため）。

※ allow_short がオフ（既定）ならポジションは見ない純粋なアラート戦略（mode=notify 向け）。
   BUY/SELL を出すだけで、paper/backtest では SELL は買い建玉の手仕舞いとして扱われる。
   allow_short をオンにすると建玉を見て、買いシグナルで「売建なら買戻し / ノーポジなら買い」、
   売りシグナルで「買建なら手仕舞い / ノーポジなら売建（SHORT）」を出す。

exit_on_trend_flip（トレンド反転で決済）を ON にすると、建玉とトレンドが逆になった足で
RSI の条件を待たずに手仕舞う（買建で短期MA<中期MA / 売建で短期MA>中期MA）。
OFF（既定）なら従来どおり反対側のシグナル（トレンド反転＋RSI のライン割れ/回復）まで持つ。
"""
from __future__ import annotations

from app.strategy.base import Context, Signal, Strategy
from app.strategy.indicators import ema, rsi, sma


def _truthy(v) -> bool:
    if isinstance(v, str):
        return v.strip().lower() not in ("false", "0", "no", "off", "")
    return bool(v)


class TrendRsiReclaim(Strategy):
    timeframe = "5m"
    description = "短期MA>中期MAでRSIが40を回復→買い通知 / 短期MA<中期MAでRSIが60を割れ→売り通知"
    default_params = {
        "ma_type": "sma",       # "sma" | "ema"
        "fast_period": 10,
        "mid_period": 30,
        "rsi_period": 14,
        "rsi_buy_level": 40.0,
        "rsi_sell_level": 60.0,
        "exit_on_trend_flip": False,
        "qty": 100,
    }
    param_meta = {
        "ma_type": {"label": "移動平均の種類", "choices": ["sma", "ema"]},
        "fast_period": {"label": "短期MA期間"},
        "mid_period": {"label": "中期MA期間"},
        "rsi_period": {"label": "RSI期間"},
        "rsi_buy_level": {"label": "買いのRSIライン",
                           "help": "短期MA>中期MAのとき、このラインを下から上に回復したら買い"},
        "rsi_sell_level": {"label": "売りのRSIライン",
                            "help": "短期MA<中期MAのとき、このラインを上から下に割ったら売り"},
        "exit_on_trend_flip": {
            "label": "トレンド反転で決済", "type": "bool",
            "help": "ON: 買建は短期MA<中期MAになった足、売建は短期MA>中期MAになった足で、"
                    "RSI の条件を待たずに手仕舞う。OFF: 反対側のシグナル（トレンド反転＋RSI）まで持つ",
        },
        "qty": {"label": "株数", "help": "1回のエントリーで売買する株数"},
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

        up = up_trend and r_prev <= buy_lv < r_now
        down = down_trend and r_prev >= sell_lv > r_now
        up_reason = f"上昇({tag}) RSI {r_prev:.0f}→{r_now:.0f} が{buy_lv:.0f}回復"
        down_reason = f"下降({tag}) RSI {r_prev:.0f}→{r_now:.0f} が{sell_lv:.0f}割れ"
        pos = ctx.position

        if _truthy(p.get("exit_on_trend_flip", False)):
            if pos.is_long and down_trend:
                return Signal("EXIT", reason=f"トレンド反転({tag}) 短期MA<中期MA で手仕舞い")
            if pos.is_short and up_trend:
                return Signal("EXIT", reason=f"トレンド反転({tag}) 短期MA>中期MA で手仕舞い")

        if self.allow_short:
            if up and pos.is_short:
                return Signal("EXIT", reason=up_reason)
            if up and pos.is_flat:
                return Signal("BUY", int(p["qty"]), reason=up_reason)
            if down and pos.is_long:
                return Signal("EXIT", reason=down_reason)
            if down and pos.is_flat:
                return Signal("SHORT", int(p["qty"]), reason=down_reason)
            return None

        if up:
            return Signal("BUY", int(p["qty"]), reason=up_reason)
        if down:
            return Signal("SELL", int(p["qty"]), reason=down_reason)
        return None
