"""ボリンジャー（拡大で順張り・縮小で逆張り）。"""
import numpy as np
import pandas as pd

from app.engine.backtest import run_backtest
from app.strategy.base import Context, Position
from app.strategy.examples.bollinger_regime import BollingerRegime
from app.strategy.registry import BUILTIN


def _bars(closes):
    idx = pd.date_range("2026-01-05 00:00", periods=len(closes), freq="5min")
    c = pd.Series(closes, index=idx, dtype=float)
    return pd.DataFrame({"open": c, "high": c + 0.05, "low": c - 0.05, "close": c, "volume": 1000.0})


def _decide(closes, params=None, pos=None):
    st = BollingerRegime(params or {})
    st.timeframe = "5m"
    b = _bars(closes)
    return st.decide(Context(symbol="X", now=b.index[-1].to_pydatetime(), bars=b, position=pos or Position()))


# 大きく揺れる → 小さく揺れる（スクイーズ）→ 上に飛ぶ（拡大して上限ブレイク）
WIDE = [100 + (2.0 if i % 2 else -2.0) for i in range(90)]
SQUEEZE = WIDE + [100 + (0.1 if i % 2 else -0.1) for i in range(50)]
BREAKOUT = SQUEEZE + [101.5]


def test_buy_on_upper_break_right_after_squeeze():
    assert _decide(SQUEEZE) is None
    sig = _decide(BREAKOUT, {"direction": "both"})
    assert sig.side == "BUY" and "上限ブレイク" in sig.reason
    assert _decide(BREAKOUT, {"use_trend": False}) is None


def test_short_on_lower_break_only_when_direction_allows():
    down = SQUEEZE + [98.5]
    assert _decide(down) is None  # 既定は買いのみ
    sig = _decide(down, {"direction": "both"})
    assert sig.side == "SHORT" and "下限ブレイク" in sig.reason


def test_exit_depends_on_whether_the_position_is_trend_or_reversion():
    after = BREAKOUT + [101.4, 100.6, 99.9]  # 上限ブレイクの後、中心線を割る
    trend = _decide(after, pos=Position(qty=100, avg_price=101.5))
    assert trend is not None and trend.side == "EXIT" and "順張り" in trend.reason
    # 逆張りの買い（建値が中心線より下）は、中心線に届いたら手仕舞う
    rev = _decide(SQUEEZE[:-1] + [100.3], pos=Position(qty=100, avg_price=99.0))
    assert rev is not None and rev.side == "EXIT" and "逆張り" in rev.reason


def test_both_modes_fire_on_random_walk_and_can_be_switched_off():
    rng = np.random.default_rng(7)
    vol = np.where(np.arange(3000) % 400 < 200, 0.3, 1.5)  # 静かな時期と荒い時期を交互に
    closes = list(1000 + np.cumsum(rng.normal(0, 1.0, 3000) * vol))
    bars = _bars(closes)

    def reasons(params):
        st = BollingerRegime({"direction": "both", **params})
        st.timeframe = "5m"
        return [t.reason_in for t in run_backtest(st, bars, "X").trades]

    both = reasons({})
    assert any("ブレイク" in r for r in both) and any("逆張り" in r for r in both)
    assert not any("逆張り" in r for r in reasons({"use_reversion": False}))
    assert not any("ブレイク" in r for r in reasons({"use_trend": False}))


def test_registered():
    assert BUILTIN["ボリンジャー（拡大で順張り・縮小で逆張り）"] == \
        "app.strategy.examples.bollinger_regime:BollingerRegime"
