"""チャート用 API（ローソク足＋エントリー/決済の印）。"""
import json
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.db import engine, init_db
from app.main import app
from app.models import BacktestRun, BacktestTrade, Bar, Order, Strategy
from app.web.chart import floor_ts, to_time

BASE = datetime(2026, 9, 29, 0, 0)  # 09:00 JST


def _bars(s, code, tf, n, step):
    init_db()
    for i in range(n):
        px = 1000.0 + i
        s.add(Bar(symbol_code=code, timeframe=tf, ts=BASE + timedelta(minutes=step * i), open=px, high=px + 2,
                  low=px - 2, close=px + 1, volume=100.0, source="rss"))


def test_time_is_jst_wall_clock_and_markers_floor_to_bar():
    # DB の 00:00 UTC ＝ JST 09:00。チャートには「09:00 を UTC とみなした」時刻を渡す（軸が JST で読める）
    assert to_time(datetime(2026, 9, 29, 0, 0)) == int(datetime(2026, 9, 29, 9, 0, tzinfo=UTC).timestamp())
    assert floor_ts(datetime(2026, 9, 29, 0, 7, 40), "5m") == datetime(2026, 9, 29, 0, 5)


def test_backtest_chart_has_candles_markers_and_moving_averages():
    with Session(engine) as s:
        _bars(s, "971A", "5m", 60, 5)
        run = BacktestRun(strategy_name="TrendRsiReclaim",
                          class_path="app.strategy.examples.trend_rsi_reclaim:TrendRsiReclaim",
                          params_json=json.dumps({"fast_period": 5, "mid_period": 10}), symbol_code="971A",
                          timeframe="5m", start=BASE, end=BASE + timedelta(hours=5), metrics_json="{}")
        s.add(run)
        s.commit()
        s.refresh(run)
        s.add(BacktestTrade(run_id=run.id, symbol_code="971A", side="SHORT",
                            entry_ts=BASE + timedelta(minutes=50), entry_price=1010,
                            exit_ts=BASE + timedelta(minutes=120), exit_price=1000, qty=100,
                            pnl=1000, return_pct=1.0))
        s.commit()
        rid = run.id
    with TestClient(app) as c:
        d = c.get(f"/api/chart/backtest/{rid}").json()
        html = c.get(f"/backtests/{rid}").text
    assert len(d["candles"]) == 60 and len(d["volume"]) == 60
    entry, exit_ = d["markers"]
    assert entry["text"] == "売" and entry["shape"] == "arrowDown" and entry["position"] == "aboveBar"
    assert exit_["text"] == "決済 +1,000" and exit_["color"] == "#FF5A5A"  # プラス=赤
    assert exit_["time"] == to_time(BASE + timedelta(minutes=120))
    assert [o["name"] for o in d["overlays"]] == ["SMA5（短期）", "SMA10（中期）"]
    assert "lightweight-charts" in html and 'data-chart-from="' in html


def test_strategy_chart_uses_real_orders_for_live():
    with Session(engine) as s:
        _bars(s, "972A", "1m", 30, 1)
        st = Strategy(name="chart-live", class_path="app.strategy.examples.sma_cross:SmaCross",
                      symbols="972A", timeframe="1m", mode="live", enabled=False, params_json="{}")
        s.add(st)
        s.commit()
        s.refresh(st)
        s.add(Order(strategy_id=st.id, symbol_code="972A", ts=BASE + timedelta(minutes=3, seconds=20),
                    side="BUY", qty=100, status="sent", ref_price=1003, avg_price=1003, idempotency_key="c1"))
        s.add(Order(strategy_id=st.id, symbol_code="972A", ts=BASE + timedelta(minutes=9, seconds=5),
                    side="EXIT", qty=100, status="sent", ref_price=1009, avg_price=1009,
                    idempotency_key="c2"))
        s.add(Order(strategy_id=st.id, symbol_code="972A", ts=BASE + timedelta(minutes=12), side="SHORT",
                    qty=100, status="rejected", ref_price=1012, idempotency_key="c3"))
        s.commit()
        sid = st.id
    with TestClient(app) as c:
        d = c.get(f"/api/chart/strategy/{sid}?days=5").json()
    texts = [m["text"] for m in d["markers"]]
    assert texts == ["買", "決済 +600", "×売（rejected）"]
    assert d["markers"][0]["time"] == to_time(BASE + timedelta(minutes=3))  # 発注時刻を含む足
    assert [o["name"] for o in d["overlays"]] == ["SMA5（短期）", "SMA20（長期）"]
