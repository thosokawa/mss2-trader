"""新高値ブレイク（NewHighBreakout）。"""
import pandas as pd

from app.strategy.base import Context, Position
from app.strategy.examples.new_high_breakout import NewHighBreakout
from app.strategy.registry import BUILTIN


def _daily(closes):
    idx = pd.date_range("2026-01-05", periods=len(closes), freq="B")  # naive UTC 00:00 = JST 9:00
    c = pd.Series(closes, index=idx, dtype=float)
    return pd.DataFrame({"open": c, "high": c + 1, "low": c - 1, "close": c, "volume": 1000.0})


def _decide(bars, params, pos=None, tf="1d"):
    st = NewHighBreakout(params)
    st.timeframe = tf
    ctx = Context(symbol="X", now=bars.index[-1].to_pydatetime(), bars=bars, position=pos or Position())
    return st.decide(ctx)


RANGE = [100 + (i % 5) for i in range(30)]  # 100〜104 で横ばい（高値は 105）


def test_buy_on_new_n_day_high_and_exit_on_m_day_low():
    p = {"lookback_days": 20, "exit_days": 5}
    assert _decide(_daily(RANGE + [104.5]), p) is None          # 20日高値 105 を超えていない
    sig = _decide(_daily(RANGE + [106]), p)
    assert sig.side == "BUY" and "20日高値 105.0 を更新" in sig.reason
    assert _decide(_daily(RANGE + [106, 107]), p) is None        # 2日目は「初めての更新」ではない
    held = Position(qty=100, avg_price=106)
    out = _decide(_daily(RANGE + [106, 107, 98]), p, pos=held)    # 5日安値（99）割れ
    assert out.side == "EXIT" and "5日安値" in out.reason


def test_new_low_short_only_when_direction_allows():
    p = {"lookback_days": 20, "exit_days": 5}
    assert _decide(_daily(RANGE + [97]), p) is None
    sig = _decide(_daily(RANGE + [97]), {**p, "direction": "both"})
    assert sig.side == "SHORT" and "20日安値" in sig.reason


def test_not_enough_days():
    assert _decide(_daily(RANGE[:10] + [120]), {"lookback_days": 20, "exit_days": 5}) is None


def test_intraday_bars_use_previous_days_high():
    """5分足: 前日までの N 日高値を、当日の足の終値が初めて超えた足で買う。"""
    days = []
    for d, base in enumerate((100, 101, 102)):
        idx = pd.date_range(f"2026-01-0{5 + d} 00:00", periods=6, freq="5min")
        days.append(pd.Series([base, base + 1, base + 2, base + 1, base, base + 1], index=idx, dtype=float))
    today = pd.Series([103, 104, 105.5, 106], index=pd.date_range("2026-01-08 00:00", periods=4, freq="5min"))
    c = pd.concat(days + [today])
    bars = pd.DataFrame({"open": c, "high": c, "low": c, "close": c, "volume": 1.0})
    p = {"lookback_days": 3, "exit_days": 0}
    assert _decide(bars.iloc[:-2], p, tf="5m") is None            # 104 は前3日の高値 104 を超えていない
    sig = _decide(bars.iloc[:-1], p, tf="5m")
    assert sig.side == "BUY" and "3日高値 104.0" in sig.reason
    assert _decide(bars, p, tf="5m") is None                      # 同じ日の次の足では建て直さない


def test_registered():
    assert BUILTIN["新高値ブレイク"] == "app.strategy.examples.new_high_breakout:NewHighBreakout"
