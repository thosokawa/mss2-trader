"""ギャップ後の押し目からの高値更新（GapBreakout）。"""
import pandas as pd

from app.strategy.base import Context, Position
from app.strategy.examples.gap_breakout import GapBreakout
from app.strategy.registry import BUILTIN


def _bars(day1, day2):
    """day1/day2: [(open, high, low, close), ...]。5分足、ts は naive UTC（00:00 = JST 9:00）。"""
    i1 = pd.date_range("2026-09-29 00:00", periods=len(day1), freq="5min")
    i2 = pd.date_range("2026-09-30 00:00", periods=len(day2), freq="5min")
    rows = [dict(zip(("open", "high", "low", "close"), r, strict=True)) for r in day1 + day2]
    df = pd.DataFrame(rows, index=i1.append(i2))
    df["volume"] = 1000.0
    return df


DAY1 = [(100, 100.5, 99.5, 100)] * 5  # 前日終値 100
UP = [(102, 103.0, 101.8, 102.8),      # ギャップアップ +2%
      (102.8, 103.5, 102.6, 103.2),    # 当日高値 103.5
      (103.2, 103.3, 102.8, 103.0),    # 押し（103.5 から 0.68%）
      (103.0, 103.9, 102.9, 103.7),    # 終値で 103.5 を更新 → 買い
      (103.7, 103.8, 102.5, 102.6)]    # 押し安値 102.8 を終値で割る → 手仕舞い


def _decide(bars, params=None, pos=None):
    st = GapBreakout(params or {})
    st.timeframe = "5m"
    return st.decide(Context(symbol="X", now=bars.index[-1].to_pydatetime(), bars=bars,
                             position=pos or Position()))


def test_buy_on_new_high_after_pullback_on_gap_up_day():
    assert _decide(_bars(DAY1, UP[:3])) is None          # まだ更新していない
    sig = _decide(_bars(DAY1, UP[:4]))
    assert sig.side == "BUY" and "ギャップアップ +2.0%" in sig.reason and "102.8" in sig.reason
    # 建てた後、同じ日にもう一度高値を更新しても建て増さない（1日1回）
    later = UP[:4] + [(103.7, 104.5, 103.6, 104.4)]
    assert _decide(_bars(DAY1, later)) is None


def test_exit_when_pullback_low_breaks():
    sig = _decide(_bars(DAY1, UP), pos=Position(qty=100, avg_price=103.7))
    assert sig.side == "EXIT" and "押し安値" in sig.reason
    assert _decide(_bars(DAY1, UP), params={"exit_on_pullback_break": False},
                   pos=Position(qty=100, avg_price=103.7)) is None


def test_no_entry_without_pullback_small_gap_or_gap_fill():
    straight = [(102, 102.5, 101.9, 102.4), (102.4, 103.0, 102.3, 102.9), (102.9, 103.6, 102.8, 103.5)]
    assert _decide(_bars(DAY1, straight)) is None                      # 押していない
    small = [(o - 1.8, h - 1.8, lo - 1.8, c - 1.8) for o, h, lo, c in UP[:4]]   # ギャップ +0.2%
    assert _decide(_bars(DAY1, small)) is None
    filled = UP[:2] + [(103.2, 103.3, 99.8, 101.0), (101.0, 103.9, 100.9, 103.7)]  # 前日終値まで戻した
    assert _decide(_bars(DAY1, filled)) is None
    assert _decide(_bars(DAY1, filled), params={"cancel_on_gap_fill": False}).side == "BUY"


def test_short_on_gap_down_day_only_when_direction_allows():
    down = [(2 * 100 - o, 2 * 100 - lo, 2 * 100 - h, 2 * 100 - c) for o, h, lo, c in UP[:4]]  # 上下を反転
    assert _decide(_bars(DAY1, down)) is None                           # 既定は買いのみ
    sig = _decide(_bars(DAY1, down), params={"direction": "both"})
    assert sig.side == "SHORT" and "ギャップダウン -2.0%" in sig.reason


def test_registered():
    assert BUILTIN["ギャップ後の高値更新"] == "app.strategy.examples.gap_breakout:GapBreakout"
