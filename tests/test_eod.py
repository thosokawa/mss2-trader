from datetime import datetime, timedelta

import pandas as pd
from sqlmodel import Session, select

from app.db import engine, init_db
from app.engine import live
from app.engine.backtest import run_backtest
from app.engine.eod import flatten_at_close, is_last_bar_of_day
from app.models import Bar, PaperTrade, Strategy, Symbol
from app.strategy.base import Context, Signal
from app.strategy.base import Strategy as BaseStrategy

JST = timedelta(hours=9)


def utc(y, m, d, hh, mm) -> datetime:
    """JST の時刻を DB 規約の naive UTC にする。"""
    return datetime(y, m, d, hh, mm) - JST


class AlwaysBuy(BaseStrategy):
    """建玉が無ければ毎足 BUY、自分からは手仕舞わない（大引け手仕舞いの検証用）。"""

    default_params = {"qty": 100}

    def on_bar(self, ctx: Context) -> Signal | None:
        return Signal("BUY", reason="always") if ctx.position.is_flat else None


# ---- 判定ロジック ------------------------------------------------------------


def test_flatten_at_close_only_when_disabled_and_intraday():
    assert not flatten_at_close({}, "5m")  # 既定は持ち越す（従来どおり）
    assert not flatten_at_close({"hold_overnight": True}, "5m")
    assert flatten_at_close({"hold_overnight": False}, "5m")
    assert flatten_at_close({"hold_overnight": "false"}, "15m")
    assert not flatten_at_close({"hold_overnight": False}, "1d")


def test_last_bar_of_day_by_cutoff():
    # 5m: 15:15〜15:20 の足が最後に動ける足
    assert is_last_bar_of_day(utc(2026, 3, 2, 15, 15), "5m")
    assert not is_last_bar_of_day(utc(2026, 3, 2, 15, 10), "5m")
    assert not is_last_bar_of_day(utc(2026, 3, 2, 11, 25), "5m")  # 前場引けは対象外
    # 15m: 15:00〜15:15 の足
    assert is_last_bar_of_day(utc(2026, 3, 2, 15, 0), "15m")
    assert not is_last_bar_of_day(utc(2026, 3, 2, 14, 45), "15m")
    # 1m: 15:19〜15:20 の足
    assert is_last_bar_of_day(utc(2026, 3, 2, 15, 19), "1m")
    assert not is_last_bar_of_day(utc(2026, 3, 2, 15, 18), "1m")
    assert not is_last_bar_of_day(utc(2026, 3, 2, 15, 15), "1d")


def test_last_bar_of_day_when_next_bar_is_another_day():
    # 15:00 引けだった頃のデータ: 14:55 の足の次が翌日
    ts = utc(2024, 3, 1, 14, 55)
    assert not is_last_bar_of_day(ts, "5m")
    assert is_last_bar_of_day(ts, "5m", next_ts=utc(2024, 3, 4, 9, 0))
    assert not is_last_bar_of_day(ts - timedelta(minutes=5), "5m", next_ts=ts)


# ---- バックテスト --------------------------------------------------------------


def _bars(times: list[datetime]) -> pd.DataFrame:
    closes = [100.0 + i for i in range(len(times))]
    return pd.DataFrame(
        {"open": closes, "high": closes, "low": closes, "close": closes, "volume": 1000.0},
        index=pd.DatetimeIndex(times),
    )


def _session(y, m, d, start: tuple[int, int], end: tuple[int, int]) -> list[datetime]:
    t, last = utc(y, m, d, *start), utc(y, m, d, *end)
    out = []
    while t <= last:
        out.append(t)
        t += timedelta(minutes=5)
    return out


def test_backtest_flattens_before_close_and_skips_entry_on_last_bar():
    times = _session(2026, 3, 2, (14, 0), (15, 25)) + _session(2026, 3, 3, (9, 0), (9, 30))
    bars = _bars(times)
    strat = AlwaysBuy({"hold_overnight": False})
    strat.timeframe = "5m"
    res = run_backtest(strat, bars, "TEST", warmup=0)

    assert len(res.trades) == 1
    t = res.trades[0]
    assert t.entry_ts == utc(2026, 3, 2, 14, 0)
    assert t.exit_ts == utc(2026, 3, 2, 15, 15)
    assert t.reason_out == "大引け手仕舞い"
    # 15:15〜15:25 の足では新規に買わない。翌日の寄りで再エントリー（建玉のまま終わる）
    assert res.equity_curve[utc(2026, 3, 2, 15, 25)] == res.equity_curve[utc(2026, 3, 2, 15, 15)]
    assert res.equity_curve[utc(2026, 3, 3, 9, 30)] != res.equity_curve[utc(2026, 3, 3, 9, 0)]


def test_backtest_holds_overnight_by_default():
    times = _session(2026, 3, 2, (14, 0), (15, 25)) + _session(2026, 3, 3, (9, 0), (9, 30))
    strat = AlwaysBuy()
    strat.timeframe = "5m"
    res = run_backtest(strat, _bars(times), "TEST", warmup=0)
    assert res.trades == []  # 一度も手仕舞わない（従来どおり）


def test_backtest_uses_next_bar_date_for_old_15_00_close():
    times = _session(2024, 3, 1, (14, 0), (14, 55)) + _session(2024, 3, 4, (9, 0), (9, 10))
    strat = AlwaysBuy({"hold_overnight": False})
    strat.timeframe = "5m"
    res = run_backtest(strat, _bars(times), "TEST", warmup=0)
    assert res.trades[0].exit_ts == utc(2024, 3, 1, 14, 55)
    assert res.trades[0].reason_out == "大引け手仕舞い"


def test_backtest_ignores_setting_for_daily_bars():
    times = [datetime(2026, 3, d) for d in range(2, 7)]
    strat = AlwaysBuy({"hold_overnight": False})
    strat.timeframe = "1d"
    res = run_backtest(strat, _bars(times), "TEST", warmup=0)
    assert res.trades == []


# ---- live（ペーパー） -----------------------------------------------------------


def _add(s: Session, code: str, times: list[datetime]) -> None:
    for i, ts in enumerate(times):
        c = 100.0 + i
        s.add(Bar(symbol_code=code, timeframe="5m", ts=ts, open=c, high=c, low=c, close=c,
                  volume=1000.0, source="rss"))
    s.commit()


def _paper_strategy(s: Session, code: str) -> Strategy:
    s.add(Symbol(code=code, name="テスト銘柄"))
    st = Strategy(
        name=f"eod-{code}",
        class_path="tests.test_eod:AlwaysBuy",
        params_json='{"qty": 100, "hold_overnight": false}',
        symbols=code,
        timeframe="5m",
        mode="paper",
        enabled=True,
    )
    s.add(st)
    s.commit()
    s.refresh(st)
    return st


def test_live_paper_flattens_before_close():
    init_db()
    with Session(engine) as s:
        st = _paper_strategy(s, "9811")
        _add(s, "9811", [utc(2026, 3, 2, 14, 0)])
        live.run_once(s, notify=False)  # カーソル初期化

        _add(s, "9811", _session(2026, 3, 2, (14, 5), (15, 25)))
        fired = live.run_once(s, notify=False)
        assert [(f.side, f.ts) for f in fired] == [
            ("BUY", utc(2026, 3, 2, 14, 5)),
            ("EXIT", utc(2026, 3, 2, 15, 15)),
        ]
        assert fired[-1].reason == "大引け手仕舞い"
        trades = s.exec(select(PaperTrade).where(PaperTrade.strategy_id == st.id)).all()
        assert len(trades) == 1 and trades[0].status == "closed"


def test_live_paper_exits_position_carried_over_to_next_day():
    init_db()
    with Session(engine) as s:
        st = _paper_strategy(s, "9812")
        _add(s, "9812", [utc(2026, 3, 2, 14, 0)])
        live.run_once(s, notify=False)  # カーソル初期化

        # 前日は 14:30 で足が途切れた（backend 停止など）→ 建玉が残ったまま翌日へ
        _add(s, "9812", _session(2026, 3, 2, (14, 5), (14, 30)))
        live.run_once(s, notify=False)
        assert live.paper.current_position(s, st.id, "9812").is_long

        _add(s, "9812", _session(2026, 3, 3, (9, 0), (9, 5)))
        fired = live.run_once(s, notify=False)
        assert [(f.side, f.ts) for f in fired] == [
            ("EXIT", utc(2026, 3, 3, 9, 0)),
            ("BUY", utc(2026, 3, 3, 9, 5)),
        ]
        assert fired[0].reason == "持ち越し建玉の手仕舞い"
