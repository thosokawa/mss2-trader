"""全戦略共通のエントリー時間帯（entry_windows）。判定は足が確定した時刻（JST）。"""
from datetime import time

import pandas as pd

from app.strategy.base import Context, Position, Signal, Strategy, parse_entry_windows
from app.strategy.registry import BUILTIN, UNIVERSAL_DEFAULTS, builtin_param_meta


class _Always(Strategy):
    """建玉が無ければ毎足 BUY、あれば毎足 EXIT。"""

    def on_bar(self, ctx):
        return Signal("BUY", 100) if ctx.position.is_flat else Signal("EXIT")


def _ctx(bar_start_jst: str, pos=None, tf_min=5):
    ts = pd.Timestamp(bar_start_jst) - pd.Timedelta(hours=9)  # DB は naive UTC
    idx = pd.date_range(end=ts, periods=3, freq=f"{tf_min}min")
    bars = pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0}, index=idx)
    return Context(symbol="X", now=ts.to_pydatetime(), bars=bars, position=pos or Position())


def test_parse():
    two = [(time(9), time(10)), (time(13, 30), time(14, 30))]
    assert parse_entry_windows("9:00-10:00 13:30-14:30") == two
    assert parse_entry_windows("09:30〜11:30") == [(time(9, 30), time(11, 30))]
    assert parse_entry_windows("") == [] and parse_entry_windows(None) == []


def test_entries_only_inside_window_by_bar_close_time():
    st = _Always({"entry_windows": "9:30-11:30"})
    st.timeframe = "5m"
    # 9:20 始まりの5分足は 9:25 確定 → 時間外 / 9:25 始まりは 9:30 確定 → 時間内
    assert st.decide(_ctx("2026-09-30 09:20")) is None
    assert st.decide(_ctx("2026-09-30 09:25")).side == "BUY"
    assert st.decide(_ctx("2026-09-30 11:25")).side == "BUY"   # 11:30 確定
    assert st.decide(_ctx("2026-09-30 13:00")) is None


def test_exits_are_not_limited_by_window():
    st = _Always({"entry_windows": "9:30-11:30"})
    st.timeframe = "5m"
    sig = st.decide(_ctx("2026-09-30 14:00", pos=Position(qty=100, avg_price=1.0)))
    assert sig is not None and sig.side == "EXIT"


def test_empty_means_all_day_and_every_strategy_has_the_field():
    st = _Always({})
    st.timeframe = "5m"
    assert st.decide(_ctx("2026-09-30 14:50")).side == "BUY"
    assert UNIVERSAL_DEFAULTS["entry_windows"] == ""
    metas = builtin_param_meta()
    for cp in BUILTIN.values():
        assert metas[cp]["entry_windows"]["label"] == "エントリー時間帯"


def test_strategy_view_shows_windows():
    from app.models import Strategy as StrategyRow
    from app.web.strategy_view import describe

    row = StrategyRow(name="w", class_path=BUILTIN["トレンド×RSI出戻り"], symbols="9984", timeframe="5m",
                      params_json='{"entry_windows": "9:00-10:00 13:30-14:30"}')
    d = describe(row)
    assert ("エントリー時間帯", "09:00-10:00 13:30-14:30") in [(a, b) for a, b, _ in d["common"]]
    assert "時間帯 09:00-10:00 13:30-14:30" in d["summary"]
