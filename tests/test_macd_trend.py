import numpy as np
import pandas as pd

from app.engine.backtest import run_backtest
from app.strategy.base import Context, Position
from app.strategy.examples.macd_trend import MacdTrendFilter
from app.strategy.indicators import resample_completed
from app.strategy.registry import BUILTIN, builtin_params


def _bars(closes, start="2026-03-02 00:00", freq="1min"):
    idx = pd.date_range(start, periods=len(closes), freq=freq)
    c = pd.Series(closes, index=idx, dtype=float)
    return pd.DataFrame({"open": c, "high": c + 0.5, "low": c - 0.5, "close": c, "volume": 1000.0})


def _wave(n=900, slope=0.05, amp=3.0, period=40):
    """一定の傾きのトレンドに短い波を乗せた1分足（波で MACD が何度もクロスする）。"""
    t = np.arange(n)
    return list(1000 + slope * t + amp * np.sin(2 * np.pi * t / period))


def _run(closes, **params):
    strat = MacdTrendFilter({"trend_tf": "15m", "trend_period": 10, "slope_lookback": 2, **params})
    strat.timeframe = "1m"
    return run_backtest(strat, _bars(closes), "X", warmup=0)


# ---- 上位足へのまとめ直し ----


def test_resample_completed_drops_forming_bar():
    bars = _bars(list(range(1, 38)))  # 00:00〜00:36 の1分足
    htf = resample_completed(bars, "15m", 1)
    # 00:00〜00:15、00:15〜00:30 は確定。00:30〜 はまだ形成中なので含めない
    assert list(htf.index.strftime("%H:%M")) == ["00:00", "00:15"]
    assert htf["open"].iloc[0] == 1 and htf["close"].iloc[0] == 15 and htf["close"].iloc[1] == 30
    # ちょうど 00:44 の足で 00:30〜00:45 が確定する
    assert list(resample_completed(_bars(list(range(45))), "15m", 1).index.strftime("%H:%M"))[-1] == "00:30"


def test_resample_completed_daily_excludes_today():
    idx = pd.date_range("2026-03-02 00:00", periods=3, freq="1D")
    bars = pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": [1.0, 2.0, 3.0], "volume": 1.0},
                        index=idx)
    assert len(resample_completed(bars, "1d", 5)) == 2


# ---- 戦略 ----


def test_registered_with_defaults():
    cp = BUILTIN["MACDクロス＋上位足トレンド"]
    assert builtin_params()[cp]["trend_tf"] == "15m"


def test_uptrend_only_buys():
    res = _run(_wave(slope=0.05), direction="both")
    sides = {t.side for t in res.trades}
    assert res.trades and sides == {"LONG"}


def test_downtrend_shorts_only_when_allowed():
    res = _run(_wave(slope=-0.05), direction="both")
    assert res.trades and {t.side for t in res.trades} == {"SHORT"}
    # 買いのみ（既定）なら下降トレンドでは何もしない
    assert _run(_wave(slope=-0.05)).trades == []


def test_no_entry_without_enough_higher_timeframe_history():
    # 15分足 EMA10 + 2本 = 12本（180分）ぶん無い間は建てない
    strat = MacdTrendFilter({"trend_tf": "15m", "trend_period": 10, "slope_lookback": 2})
    strat.timeframe = "1m"
    bars = _bars(_wave(n=150))
    trend, why = strat.trend(Context("X", bars.index[-1], bars, Position(), strat.params))
    assert trend == 0 and "本数不足" in why


def test_higher_tf_must_be_larger_than_trading_tf():
    strat = MacdTrendFilter({"trend_tf": "5m"})
    strat.timeframe = "15m"
    bars = _bars(_wave(n=400), freq="15min")
    trend, why = strat.trend(Context("X", bars.index[-1], bars, Position(), strat.params))
    assert trend == 0 and "以下" in why


def test_exit_on_trend_flip():
    """建玉があるとき、上位足のトレンドが崩れたら MACD の逆クロスを待たずに手仕舞う（オフなら待つ）。"""
    bars = _bars(_wave(n=400, slope=0.05))
    # MACD がクロスしていない足（最後の足で上抜けも下抜けも起きていない）を探す
    from app.strategy.indicators import crossed_down, crossed_up, macd
    def no_cross(k):
        line, sig, _ = macd(bars["close"].iloc[:k])
        return not crossed_up(line, sig) and not crossed_down(line, sig)

    n = next(k for k in range(len(bars), 60, -1) if no_cross(k))
    window = bars.iloc[:n]
    for flip, want in ((True, "EXIT"), (False, None)):
        strat = MacdTrendFilter({"exit_on_trend_flip": flip})
        strat.timeframe = "1m"
        strat.trend = lambda ctx: (-1, "15m 下降")  # 上位足が下降に転じた
        sig = strat.on_bar(Context("X", window.index[-1], window, Position(100, 1000.0), strat.params))
        assert (sig.side if sig else None) == want
        if sig:
            assert "トレンドが崩れた" in sig.reason
