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

エントリーの絞り込み（2026-09-30 の分析で、この2つで勝ち負けがはっきり分かれた。既定はどちらも無効）:
- gap_filter（ギャップの向きにだけ建てる）: 当日の始値が前日終値より上（ギャップアップ）の日は買いだけ、
  下（ギャップダウン）の日は売りだけ。前日の足が無いときは絞らない。
- min_pullback（押し目の最低の深さ）: 直近12本の RSI が 買いライン−この値 以下（売りは 売りライン＋この値
  以上）まで行ってからの回復・割れだけで建てる。0 なら従来どおり（ラインを跨げば建てる）。

決済は既定で反対側のシグナル（トレンド反転＋RSI のライン割れ/回復）。全戦略共通の「決済条件」を
SMAクロス にすると SMA のクロスで手仕舞う（app/strategy/base.py の Strategy.decide）。
"""
from __future__ import annotations

import pandas as pd

from app.strategy.base import Context, Signal, Strategy
from app.strategy.indicators import ema, rsi, sma


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
        "gap_filter": False,
        "min_pullback": 0.0,
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
        "gap_filter": {
            "label": "ギャップの向きにだけ建てる", "type": "bool",
            "help": "ON: 当日の始値が前日終値より上の日は買いだけ、下の日は売りだけ建てる"
                    "（手仕舞いは制限しない）",
        },
        "min_pullback": {
            "label": "押し目の最低の深さ(RSI)", "type": "number",
            "help": "直近12本の RSI が 買いライン−この値 以下（売りは 売りライン＋この値 以上）まで"
                    "行ってからの回復・割れだけで建てる。例 4 なら買いは RSI 36 以下まで押してから 40 回復。"
                    "0 で制限なし",
        },
        "qty": {"label": "株数", "help": "1回のエントリーで売買する株数"},
    }

    PULLBACK_LOOKBACK = 12  # 押し目の深さを見る本数

    def _gap(self, ctx: Context) -> int:
        """当日のギャップの向き: +1=ギャップアップ / -1=ギャップダウン / 0=なし・前日の足が無い。
        1日1回だけ計算して使い回す（バックテストで毎足計算すると遅いため）。"""
        idx = ctx.bars.index
        day = (pd.Timestamp(idx[-1]) + JST).normalize()
        cache = getattr(self, "_gap_cache", None)
        if cache and cache[0] == day:
            return cache[1]
        jst_days = (idx + JST).normalize()
        today = jst_days == day
        gap = 0
        if today.any() and not today.all():
            first = int(today.argmax())
            prev_close = float(ctx.bars["close"].iloc[first - 1]) if first > 0 else None
            today_open = float(ctx.bars["open"].iloc[first])
            if prev_close:
                gap = (today_open > prev_close) - (today_open < prev_close)
        self._gap_cache = (day, gap)
        return gap

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

        # エントリーの絞り込み（手仕舞いには使わない）
        can_buy = can_short = True
        depth = float(p.get("min_pullback") or 0)
        if depth > 0:
            recent = r.iloc[-self.PULLBACK_LOOKBACK:]
            can_buy = float(recent.min()) <= buy_lv - depth
            can_short = float(recent.max()) >= sell_lv + depth
        if _truthy(p.get("gap_filter", False)):
            gap = self._gap(ctx)
            can_buy = can_buy and gap >= 0
            can_short = can_short and gap <= 0

        if self.allow_short:
            if up and pos.is_short:
                return Signal("EXIT", reason=up_reason)
            if up and pos.is_flat and can_buy:
                return Signal("BUY", int(p["qty"]), reason=up_reason)
            if down and pos.is_long:
                return Signal("EXIT", reason=down_reason)
            if down and pos.is_flat and can_short:
                return Signal("SHORT", int(p["qty"]), reason=down_reason)
            return None

        if up and can_buy:
            return Signal("BUY", int(p["qty"]), reason=up_reason)
        if down:
            return Signal("SELL", int(p["qty"]), reason=down_reason)
        return None


JST = pd.Timedelta(hours=9)


def _truthy(v) -> bool:
    if isinstance(v, str):
        return v.strip().lower() not in ("false", "0", "no", "off", "")
    return bool(v)
