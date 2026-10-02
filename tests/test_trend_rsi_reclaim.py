import numpy as np
import pandas as pd

from app.strategy.base import Context, Position
from app.strategy.examples.trend_rsi_reclaim import TrendRsiReclaim


def _bars(closes: list[float]) -> pd.DataFrame:
    idx = pd.date_range("2026-01-05 09:00", periods=len(closes), freq="5min")
    c = pd.Series(closes, index=idx, dtype=float)
    return pd.DataFrame({"open": c, "high": c + 1, "low": c - 1, "close": c, "volume": 1000.0})


def _sig(closes: list[float], params: dict | None = None, position: Position | None = None):
    strat = TrendRsiReclaim(params or {})
    bars = _bars(closes)
    ctx = Context(symbol="X", now=bars.index[-1].to_pydatetime(), bars=bars, position=position or Position())
    return strat.on_bar(ctx)


# 強い上昇 → 急な押し目で RSI が 40 割れ → 1本戻して 40 回復（fast>mid 維持）
BUY_CLOSES = (
    list(np.arange(100, 100 + 80 * 1.2, 1.2))
    + list(np.arange(100, 100 + 80 * 1.2, 1.2)[-1] + np.cumsum([-3.5] * 7))
)
BUY_CLOSES = BUY_CLOSES + [BUY_CLOSES[-1] + 5.0]

DOWN = list(np.arange(220, 220 - 80 * 1.2, -1.2))
SELL_CLOSES = DOWN + list(DOWN[-1] + np.cumsum([3.5] * 7))
SELL_CLOSES = SELL_CLOSES + [SELL_CLOSES[-1] - 5.0]


def test_buy_on_rsi_reclaim_in_uptrend():
    sig = _sig(BUY_CLOSES)
    assert sig is not None and sig.side == "BUY"
    assert "40回復" in sig.reason


def test_sell_on_rsi_breakdown_in_downtrend():
    sig = _sig(SELL_CLOSES)
    assert sig is not None and sig.side == "SELL"
    assert "60割れ" in sig.reason


def test_no_signal_without_reclaim():
    # まだ RSI が 40 を回復していない足（押し目の途中）
    partial = BUY_CLOSES[:-1]  # 最後の戻し1本を除く
    assert _sig(partial) is None


def test_trend_filter_blocks_opposite():
    # 上昇の RSI 回復パターンでも、売りレベルを跨がないので SELL は出ない
    sig = _sig(BUY_CLOSES)
    assert sig.side != "SELL"
    # 下降トレンドで RSI が 40 を回復しても BUY は出ない（up_trend が False）
    assert _sig(SELL_CLOSES).side != "BUY"


def test_warmup_guard():
    assert _sig([100.0] * 20) is None


def _two_day_bars(closes, split, day2_open=None):
    """前半 split 本を前日、残りを当日の5分足にする（ts は naive UTC、00:00=JST 9:00）。"""
    d1 = pd.date_range("2026-09-28 00:00", periods=split, freq="5min")
    d2 = pd.date_range("2026-09-29 00:00", periods=len(closes) - split, freq="5min")
    c = pd.Series(closes, index=d1.append(d2), dtype=float)
    df = pd.DataFrame({"open": c, "high": c + 1, "low": c - 1, "close": c, "volume": 1000.0})
    if day2_open is not None:
        df.iloc[split, df.columns.get_loc("open")] = day2_open
    return df


def _sig_bars(bars, params):
    strat = TrendRsiReclaim(params)
    ctx = Context(symbol="X", now=bars.index[-1].to_pydatetime(), bars=bars, position=Position())
    return strat.on_bar(ctx)


def test_gap_filter_blocks_buy_on_gap_down_day():
    split = 60
    up_day = _two_day_bars(BUY_CLOSES, split)  # 当日の始値 > 前日終値（上昇の途中で日付が変わる）
    assert _sig_bars(up_day, {"gap_filter": True}).side == "BUY"
    down_day = _two_day_bars(BUY_CLOSES, split, day2_open=BUY_CLOSES[split - 1] - 10)  # ギャップダウン
    assert _sig_bars(down_day, {"gap_filter": True}) is None
    assert _sig_bars(down_day, {"gap_filter": False}).side == "BUY"  # 既定は絞らない


def test_min_pullback_requires_deeper_rsi_dip():
    import app.strategy.indicators as ind

    bars = _bars(BUY_CLOSES)
    lowest = float(ind.rsi(bars["close"], 14).iloc[-12:].min())
    enough = 40 - lowest - 0.5   # 実際の押しより浅い条件 → 建てる
    too_deep = 40 - lowest + 0.5  # 実際の押しより深い条件 → 建てない
    assert _sig(BUY_CLOSES, {"min_pullback": enough}).side == "BUY"
    assert _sig(BUY_CLOSES, {"min_pullback": too_deep}) is None
    assert _sig(BUY_CLOSES, {"min_pullback": 0}).side == "BUY"


def test_rsi_fail_stop_exits_when_rsi_crosses_back():
    """RSIの出戻り失敗で損切り: 買建は RSI が買いラインを再び下抜けたら、売建は売りラインを再び上抜けたら。"""
    import app.strategy.indicators as ind

    # 買い（40 回復）の後、また下げて RSI が 40 を割る足を探す
    after = BUY_CLOSES + [BUY_CLOSES[-1] - 3.0 * k for k in range(1, 6)]
    r = ind.rsi(pd.Series(after, dtype=float), 14)
    k = next(i for i in range(len(BUY_CLOSES), len(after)) if r.iloc[i - 1] >= 40 > r.iloc[i])
    pos = Position(qty=100, avg_price=BUY_CLOSES[-1])
    sig = _sig(after[: k + 1], {"exit_on_rsi_fail": True}, position=pos)
    assert sig is not None and sig.side == "EXIT" and sig.is_stop and "出戻り失敗" in sig.reason
    off = _sig(after[: k + 1], {}, position=pos)
    assert off is None or not off.is_stop  # 既定（OFF）では損切りとしては出ない
    # 決済条件=SMAクロスでも効く（decide 経由。この足で SMA の手仕舞いが先に当たらないよう SMA 側は止める）
    st = TrendRsiReclaim({"exit_on_rsi_fail": True, "exit_rule": "sma_cross"})
    st._sma_cross_exit = lambda ctx: None
    b = _bars(after[: k + 1])
    got = st.decide(Context(symbol="X", now=b.index[-1].to_pydatetime(), bars=b, position=pos))
    assert got is not None and got.is_stop

    # 売り（60 割れ）の後に戻して RSI が 60 を超える
    after_s = SELL_CLOSES + [SELL_CLOSES[-1] + 3.0 * k for k in range(1, 6)]
    rs = ind.rsi(pd.Series(after_s, dtype=float), 14)
    k2 = next(i for i in range(len(SELL_CLOSES), len(after_s)) if rs.iloc[i - 1] <= 60 < rs.iloc[i])
    sig = _sig(after_s[: k2 + 1], {"exit_on_rsi_fail": True, "direction": "both"},
               position=Position(qty=-100, avg_price=SELL_CLOSES[-1]))
    assert sig is not None and sig.side == "EXIT" and sig.is_stop
