"""MACD クロス × 上位足トレンドへの順張り。

売買する足（戦略の「足」。例 1m / 5m）の MACD クロスでタイミングを取り、上位足（trend_tf。例 15m / 60m）
のトレンドと同じ向きのときだけ建てる。上位足は売買する足を戦略の中でまとめ直して作る
（indicators.resample_completed。確定した上位足だけを使うので未来の値を見ない）。
バックテストとライブで同じ計算になる。

エントリー:
  買い(BUY)    … 上位足が上昇トレンド、かつ MACD がシグナルを上抜け
  売建(SHORT)  … 上位足が下降トレンド、かつ MACD がシグナルを下抜け（売買方向が「売りのみ」「両方」のとき）
上位足のトレンド（trend_rule）:
  "price"  … 上位足の終値が EMA(trend_period) より上（下）
  "slope"  … 上位足の EMA が slope_lookback 本前より上向き（下向き）
  "both"   … 両方（既定。だましを減らす）
エグジット: MACD の逆クロス。exit_on_trend_flip=ON なら上位足のトレンドが崩れた時点でも手仕舞い。

必要な過去足: 上位足の EMA を作るぶん（例 15分足で EMA20 なら 1分足で約 300 本 ＝ 1〜2 営業日）。
足りない間はシグナルを出さない。
"""
from __future__ import annotations

from app.strategy.base import Context, Signal, Strategy
from app.strategy.indicators import TIMEFRAME_MINUTES, crossed_down, crossed_up, ema, macd, resample_completed


class MacdTrendFilter(Strategy):
    timeframe = "1m"
    description = ("上位足のトレンドと同じ向きのときだけ、MACDクロスで建てる順張り"
                   "（例: 1分足の MACD × 15分足の EMA トレンド）。"
                   "手仕舞いは MACD の逆クロス、または上位足のトレンドの崩れ")
    default_params = {
        "fast": 12,
        "slow": 26,
        "signal": 9,
        "trend_tf": "15m",
        "trend_period": 20,
        "trend_rule": "both",
        "slope_lookback": 3,
        "exit_on_trend_flip": True,
        "qty": 100,
    }
    param_meta = {
        "fast": {"label": "MACD短期期間"},
        "slow": {"label": "MACD長期期間"},
        "signal": {"label": "シグナル期間"},
        "trend_tf": {
            "label": "上位足", "choices": ["5m", "15m", "30m", "60m", "1d"],
            "choice_labels": {"5m": "5分足", "15m": "15分足", "30m": "30分足", "60m": "60分足", "1d": "日足"},
            "help": "トレンドを判定する大きい足。売買する足より大きいものを選ぶ",
        },
        "trend_period": {"label": "上位足EMA期間", "help": "上位足のトレンドを判定する EMA の本数"},
        "trend_rule": {
            "label": "トレンドの判定", "choices": ["both", "price", "slope"],
            "choice_labels": {"both": "終値がEMAの上 かつ EMAが上向き", "price": "終値がEMAの上",
                              "slope": "EMAが上向き"},
            "help": "上昇トレンドの条件（下降はその逆）",
        },
        "slope_lookback": {
            "label": "EMAの向きを見る本数",
            "help": "上位足の EMA を何本前と比べて上向き/下向きと判定するか",
        },
        "exit_on_trend_flip": {
            "label": "トレンドが崩れたら手仕舞い",
            "help": "オンにすると、MACD の逆クロスを待たずに上位足のトレンドが崩れた時点で手仕舞い",
        },
        "qty": {"label": "株数", "help": "1回のエントリーで売買する株数"},
    }

    def trend(self, ctx: Context) -> tuple[int, str]:
        """上位足のトレンド: (+1=上昇 / -1=下降 / 0=どちらでもない・データ不足, 説明)。"""
        p = self.params
        tf = str(p["trend_tf"])
        period, look = int(p["trend_period"]), int(p["slope_lookback"])
        base_min = TIMEFRAME_MINUTES.get(self.timeframe, 5)
        if TIMEFRAME_MINUTES.get(tf, 0) <= base_min:
            return 0, f"上位足 {tf} が売買する足 {self.timeframe} 以下"
        htf = resample_completed(ctx.bars, tf, base_min)
        if len(htf) < period + look:
            return 0, f"上位足 {tf} の本数不足（{len(htf)}/{period + look}）"
        e = ema(htf["close"], period)
        close, e_now, e_prev = float(htf["close"].iloc[-1]), float(e.iloc[-1]), float(e.iloc[-1 - look])
        rule = p["trend_rule"]
        up_price, down_price = close > e_now, close < e_now
        up_slope, down_slope = e_now > e_prev, e_now < e_prev
        if rule == "price":
            up, down = up_price, down_price
        elif rule == "slope":
            up, down = up_slope, down_slope
        else:
            up, down = up_price and up_slope, down_price and down_slope
        slope_txt = "上向き" if up_slope else "下向き" if down_slope else "横ばい"
        desc = f"{tf} 終値{close:.1f}/EMA{period} {e_now:.1f}（{slope_txt}）"
        return (1 if up else -1 if down else 0), desc

    def on_bar(self, ctx: Context) -> Signal | None:
        p = self.params
        c = ctx.close
        fast_n, slow_n, sig_n = int(p["fast"]), int(p["slow"]), int(p["signal"])
        if len(c) < slow_n + sig_n + 2:
            return None
        line, sig, _ = macd(c, fast_n, slow_n, sig_n)
        trend, tdesc = self.trend(ctx)
        m = f"MACD {line.iloc[-1]:.2f}/{sig.iloc[-1]:.2f}"
        pos = ctx.position

        if pos.is_long:
            if crossed_down(line, sig):
                return Signal("EXIT", reason=f"MACD下抜け {m}")
            if p["exit_on_trend_flip"] and trend <= 0:
                return Signal("EXIT", reason=f"上位足の上昇トレンドが崩れた {tdesc}")
            return None
        if pos.is_short:
            if crossed_up(line, sig):
                return Signal("EXIT", reason=f"MACD上抜け {m}")
            if p["exit_on_trend_flip"] and trend >= 0:
                return Signal("EXIT", reason=f"上位足の下降トレンドが崩れた {tdesc}")
            return None

        if trend > 0 and crossed_up(line, sig):
            return Signal("BUY", int(p["qty"]), reason=f"上昇トレンド中のMACD上抜け {m} / {tdesc}")
        if trend < 0 and self.allow_short and crossed_down(line, sig):
            return Signal("SHORT", int(p["qty"]), reason=f"下降トレンド中のMACD下抜け {m} / {tdesc}")
        return None
