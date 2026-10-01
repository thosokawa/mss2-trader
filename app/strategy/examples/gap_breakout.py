"""ギャップ後の押し目からの高値更新（デイトレ）。

寄り付きが前日終値から上に窓を開けた（ギャップアップ）日に、いったん高値を付けて押したあと、
その日の高値を再び更新したら買う。ギャップダウンの日は逆に、戻したあと安値を更新したら売る。

- ギャップ: 当日の始値と前日の最後の足の終値の差が gap_min_pct（%）以上
- 押し: その日の高値（ブレイク前）から pullback_pct（%）以上下げてから
- 高値更新: 足の終値がそれまでの当日高値を上回った足（ヒゲだけの更新は見ない）。1日1回まで
- 窓埋めで中止（cancel_on_gap_fill）: ブレイク前に前日終値まで戻ったらその日は建てない
- 手仕舞い: 押し安値（売りは戻り高値）を終値で割ったら「ブレイク失敗」で手仕舞い（exit_on_pullback_break）。
  それ以外は大引け前の手仕舞い（大引けをまたぐ=OFF のとき）や、全戦略共通の損切り・利確・決済条件で

足は 1m/5m/15m の日中足向け（日足では意味がない）。前日の足が無い最初の日は何もしない。
"""
from __future__ import annotations

import pandas as pd

from app.strategy.base import Context, Signal, Strategy

JST = pd.Timedelta(hours=9)


def _truthy(v) -> bool:
    if isinstance(v, str):
        return v.strip().lower() not in ("false", "0", "no", "off", "")
    return bool(v)


def find_setup(today: pd.DataFrame, prev_close: float, gap_up: bool, pullback_pct: float,
               cancel_on_fill: bool) -> tuple[int, float] | None:
    """当日の足から、最初の「押してからの高値更新」（ギャップダウンなら戻してからの安値更新）を探す。

    戻り値: (ブレイクした足の位置, 押し安値 / 戻り高値)。まだ無ければ None。
    ギャップアップの日の考え方（ダウンはすべて逆）:
      それまでの当日高値 H と、H を付けた後の最安値 L を持ちながら1本ずつ進み、
      L が H×(1−押し%) 以下まで下げていて、その足の終値が H を上回ったらブレイク。
      高値を付けた足の中では高値と安値のどちらが先か分からないので、その足の安値は押しに数えず
      終値から数える（寄り付きの足の安値を「高値の後の押し」と誤認しないため）。
    """
    hi = today["high"].to_numpy()
    lo = today["low"].to_numpy()
    cl = today["close"].to_numpy()
    need = pullback_pct / 100.0
    if gap_up:
        peak, trough = hi[0], cl[0]
        for i in range(1, len(today)):
            if cancel_on_fill and min(trough, lo[i]) <= prev_close:
                return None  # 窓を埋めた＝ギャップ失敗
            if trough <= peak * (1 - need) and cl[i] > peak:
                return i, trough
            if hi[i] > peak:
                peak, trough = hi[i], cl[i]
            else:
                trough = min(trough, lo[i])
    else:
        bottom, crest = lo[0], cl[0]
        for i in range(1, len(today)):
            if cancel_on_fill and max(crest, hi[i]) >= prev_close:
                return None
            if crest >= bottom * (1 + need) and cl[i] < bottom:
                return i, crest
            if lo[i] < bottom:
                bottom, crest = lo[i], cl[i]
            else:
                crest = max(crest, hi[i])
    return None


class GapBreakout(Strategy):
    timeframe = "5m"
    description = ("ギャップアップの日に押してから当日高値を更新したら買い / "
                   "ギャップダウンの日に戻してから当日安値を更新したら売り（デイトレ）")
    default_params = {
        "gap_min_pct": 0.5,
        "pullback_pct": 0.3,
        "cancel_on_gap_fill": True,
        "exit_on_pullback_break": True,
        "qty": 100,
    }
    param_meta = {
        "gap_min_pct": {"label": "ギャップの最小幅(%)",
                        "help": "当日の始値が前日終値からこの%以上離れて始まった日だけ狙う"},
        "pullback_pct": {"label": "押しの最小幅(%)",
                         "help": "当日高値からこの%以上下げて（売りは安値から戻して）から"
                                 "更新したときだけ建てる"},
        "cancel_on_gap_fill": {"label": "窓埋めで中止", "type": "bool",
                               "help": "ON: 高値更新の前に前日終値まで戻したら（窓を埋めたら）"
                                       "その日は建てない"},
        "exit_on_pullback_break": {"label": "押し安値割れで手仕舞い", "type": "bool",
                                   "help": "ON: 押し安値（売りは戻り高値）を終値で割ったら"
                                           "ブレイク失敗として手仕舞う"},
        "qty": {"label": "株数", "help": "1回のエントリーで売買する株数"},
    }

    def on_bar(self, ctx: Context) -> Signal | None:
        p = self.params
        bars = ctx.bars
        if len(bars) < 3:
            return None
        days = (bars.index + JST).normalize()
        today_mask = days == days[-1]
        first = int(today_mask.argmax())
        if first == 0:
            return None  # 前日の足が無い
        today = bars[today_mask]
        prev_close = float(bars["close"].iloc[first - 1])
        day_open = float(today["open"].iloc[0])
        gap_pct = (day_open / prev_close - 1) * 100 if prev_close else 0.0
        if abs(gap_pct) < float(p["gap_min_pct"]) or len(today) < 2:
            return None
        gap_up = gap_pct > 0
        setup = find_setup(today, prev_close, gap_up, float(p["pullback_pct"]),
                           _truthy(p.get("cancel_on_gap_fill", True)))
        if setup is None:
            return None
        i, level = setup
        last = len(today) - 1
        close = float(today["close"].iloc[-1])
        pos = ctx.position
        gap_txt = f"ギャップ{'アップ' if gap_up else 'ダウン'} {gap_pct:+.1f}%"

        # 手仕舞い: 押し安値（戻り高値）を終値で割ったらブレイク失敗
        if _truthy(p.get("exit_on_pullback_break", True)) and i < last:
            if pos.is_long and gap_up and close < level:
                return Signal("EXIT", reason=f"押し安値 {level:,.1f} 割れ（ブレイク失敗）")
            if pos.is_short and not gap_up and close > level:
                return Signal("EXIT", reason=f"戻り高値 {level:,.1f} 超え（ブレイク失敗）")

        if i != last or not pos.is_flat:
            return None  # ブレイクはその日の最初の1回だけ・その足で建てる
        if gap_up:
            return Signal("BUY", int(p["qty"]), reason=f"{gap_txt}・押し安値 {level:,.1f} から当日高値更新")
        return Signal("SHORT", int(p["qty"]), reason=f"{gap_txt}・戻り高値 {level:,.1f} から当日安値更新")
