"""新高値ブレイク（N日高値の更新で買い）。

過去 N 日（営業日）の高値を終値で上抜けたら買う。売買方向が「両方/売りのみ」なら、過去 N 日の安値を
終値で下抜けたら売る（新安値ブレイク）。手仕舞いは過去 M 日の安値（売りは高値）を終値で割ったら。

- 日足ならスイング（例 N=250 で52週高値＝年初来高値の目安、M=20）
- 日中足（5分足など）ならデイトレ（例 N=20 で20日高値を場中に更新したら。大引けをまたがない設定と併用）

N 日・M 日の高値・安値は、売買している足を日付（JST）ごとにまとめて作る（当日は含めない）。
1日1回だけ計算して使い回す（バックテストで毎足計算すると遅いため）。
"""
from __future__ import annotations

import pandas as pd

from app.strategy.base import Context, Signal, Strategy

JST = pd.Timedelta(hours=9)


class NewHighBreakout(Strategy):
    timeframe = "1d"
    description = "過去N日の高値を終値で更新したら買い（新安値なら売り）。過去M日の安値割れで手仕舞い"
    default_params = {
        "lookback_days": 60,
        "exit_days": 10,
        "qty": 100,
    }
    param_meta = {
        "lookback_days": {"label": "新高値の日数(N)",
                          "help": "過去この日数（営業日）の高値を終値で上抜けたら買い。250 で52週高値の目安"},
        "exit_days": {"label": "手仕舞いの日数(M)",
                      "help": "過去この日数の安値（売りは高値）を終値で割ったら手仕舞い。0 で使わない"},
        "qty": {"label": "株数", "help": "1回のエントリーで売買する株数"},
    }

    def _levels(self, ctx: Context) -> tuple[float, float, float, float] | None:
        """(N日高値, N日安値, M日安値, M日高値)。前日までの日ごとの高値・安値から。足りなければ None。"""
        idx = ctx.bars.index
        day = (pd.Timestamp(idx[-1]) + JST).normalize()
        cache = getattr(self, "_lv_cache", None)
        if cache and cache[0] == day:
            return cache[1]
        n = int(self.params["lookback_days"])
        m = int(self.params.get("exit_days") or 0)
        out = None
        if self.timeframe == "1d":
            # 日足はそのまま（1本＝1日）。前日まで＝最後の1本を除く、必要な本数だけ見る
            daily = ctx.bars.iloc[-(max(n, m, 1) + 1):-1][["high", "low"]]
        else:
            days = (idx + JST).normalize()
            prev = ctx.bars[days < day]
            daily = prev
            if not prev.empty:
                daily = prev.groupby((prev.index + JST).normalize()).agg(high=("high", "max"),
                                                                       low=("low", "min"))
        if not daily.empty:
            if len(daily) >= max(n, m, 1):
                hi_n, lo_n = float(daily["high"].iloc[-n:].max()), float(daily["low"].iloc[-n:].min())
                lo_m = float(daily["low"].iloc[-m:].min()) if m > 0 else float("nan")
                hi_m = float(daily["high"].iloc[-m:].max()) if m > 0 else float("nan")
                out = (hi_n, lo_n, lo_m, hi_m)
        self._lv_cache = (day, out)
        return out

    def on_bar(self, ctx: Context) -> Signal | None:
        if len(ctx.bars) < 2:
            return None
        lv = self._levels(ctx)
        if lv is None:
            return None
        hi_n, lo_n, lo_m, hi_m = lv
        n = int(self.params["lookback_days"])
        m = int(self.params.get("exit_days") or 0)
        close = float(ctx.bars["close"].iloc[-1])
        prev_close = float(ctx.bars["close"].iloc[-2])
        pos = ctx.position

        if pos.is_long and m > 0 and close < lo_m:
            return Signal("EXIT", reason=f"{m}日安値 {lo_m:,.1f} 割れ")
        if pos.is_short and m > 0 and close > hi_m:
            return Signal("EXIT", reason=f"{m}日高値 {hi_m:,.1f} 超え")
        if not pos.is_flat:
            return None
        qty = int(self.params["qty"])
        # 前の足ではまだ超えていなかった＝この足で初めて更新した（同じ日に何度も建てない）
        if close > hi_n and prev_close <= hi_n:
            return Signal("BUY", qty, reason=f"{n}日高値 {hi_n:,.1f} を更新（終値 {close:,.1f}）")
        if close < lo_n and prev_close >= lo_n:
            return Signal("SHORT", qty, reason=f"{n}日安値 {lo_n:,.1f} を更新（終値 {close:,.1f}）")
        return None
