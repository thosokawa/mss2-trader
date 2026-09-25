import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def test_dashboard_ok(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "ダッシュボード" in r.text


def test_symbol_set_crud(client):
    client.post("/symbol-sets", data={"name": "テストセット", "note": ""})
    r = client.get("/symbol-sets")
    assert "テストセット" in r.text
    # 直近で作られたセットの詳細に銘柄追加
    import re

    set_id = max(int(x) for x in re.findall(r"/symbol-sets/(\d+)\"", r.text))
    r = client.post(
        f"/symbol-sets/{set_id}/items",
        data={"code": "7203", "name": "トヨタ自動車"},
        follow_redirects=True,
    )
    assert "7203" in r.text and "トヨタ自動車" in r.text


def test_strategies_prefill_from_query(client):
    r = client.get(
        "/strategies",
        params={
            "class_path": "app.strategy.examples.sma_cross:SmaCross",
            "params_json": '{"fast": 7, "slow": 21, "qty": 50}',
        },
    )
    assert r.status_code == 200
    assert "fast&#34;: 7" in r.text
    assert "slow&#34;: 21" in r.text


def test_strategy_crud(client):
    import re

    client.post("/symbol-sets", data={"name": "戦略用セット", "note": ""})
    r = client.get("/symbol-sets")
    set_id = max(int(x) for x in re.findall(r"/symbol-sets/(\d+)\"", r.text))

    client.post(
        "/strategies",
        data={
            "name": "SMAテスト戦略",
            "class_path": "app.strategy.examples.sma_cross:SmaCross",
            "symbol_set_id": str(set_id),
            "timeframe": "5m",
            "params_json": '{"fast": 5, "slow": 20, "qty": 100}',
            "mode": "notify",
        },
        follow_redirects=True,
    )
    r = client.get("/strategies")
    assert "SMAテスト戦略" in r.text
    assert "停止" in r.text  # 既定は無効

    strategy_id = max(int(x) for x in re.findall(r"/strategies/(\d+)/toggle", r.text))
    r = client.post(f"/strategies/{strategy_id}/toggle", follow_redirects=True)
    assert "稼働中" in r.text

    r = client.post(f"/strategies/{strategy_id}/delete", follow_redirects=True)
    assert "SMAテスト戦略" not in r.text


def test_backtest_form_state_persists_after_run(client):
    from datetime import datetime, timedelta

    from sqlmodel import Session

    from app.db import engine
    from app.models import Bar, Symbol

    with Session(engine) as s:
        s.add(Symbol(code="9984", name="ソフトバンクG"))
        base = datetime(2026, 5, 1)
        closes = [100 - i * 0.4 for i in range(40)] + [80 + i for i in range(40)]
        for i, price in enumerate(closes):
            s.add(
                Bar(
                    symbol_code="9984", timeframe="15m", ts=base + timedelta(minutes=15 * i),
                    open=price, high=price, low=price, close=price, volume=100.0, source="test",
                )
            )
        s.commit()

    r = client.post(
        "/backtest",
        data={
            "class_path": "app.strategy.examples.ma_rsi:MaRsi",
            "symbol_code": "9984",
            "timeframe": "15m",
            "params_json": '{"ma_period": 20, "rsi_period": 10, "qty": 200}',
            "commission_per_trade": "5",
        },
    )
    assert r.status_code == 200
    # 実行後も、選んだ戦略・銘柄・足・パラメータが選択されたまま（初期値に戻らない）
    assert 'app.strategy.examples.ma_rsi:MaRsi" selected' in r.text
    assert 'value="9984" selected' in r.text
    assert "selected>15m" in r.text
    assert "ma_period&#34;: 20" in r.text

    # JSON エラー時も入力内容が失われない
    r = client.post(
        "/backtest",
        data={
            "class_path": "app.strategy.examples.ma_rsi:MaRsi",
            "symbol_code": "9984",
            "timeframe": "15m",
            "params_json": "not-json",
            "commission_per_trade": "0",
        },
    )
    assert r.status_code == 200
    assert 'app.strategy.examples.ma_rsi:MaRsi" selected' in r.text
    assert 'value="9984" selected' in r.text


def test_optimize_invalid_grid_json(client):
    r = client.post(
        "/optimize",
        data={
            "class_path": "app.strategy.examples.sma_cross:SmaCross",
            "symbol_code": "7203",
            "timeframe": "5m",
            "grid_json": "not-json",
            "train_ratio": "0.7",
            "min_test_trades": "3",
            "rank_by": "total_pnl",
            "commission_per_trade": "0",
        },
    )
    assert r.status_code == 200
    assert "JSON エラー" in r.text


def test_optimize_end_to_end(client):
    import json
    import re
    from datetime import datetime, timedelta

    from sqlmodel import Session

    from app.db import engine
    from app.models import Bar

    with Session(engine) as s:
        base = datetime(2026, 5, 1, 0, 0)
        closes = [100 - i * 0.5 for i in range(40)] + [80 + i for i in range(40)]
        for i, price in enumerate(closes):
            s.add(
                Bar(
                    symbol_code="9999", timeframe="5m", ts=base + timedelta(minutes=5 * i),
                    open=price, high=price, low=price, close=price, volume=100.0, source="test",
                )
            )
        s.commit()

    r = client.post(
        "/optimize",
        data={
            "class_path": "app.strategy.examples.sma_cross:SmaCross",
            "symbol_code": "9999",
            "timeframe": "5m",
            "grid_json": json.dumps({"fast": [5, 10], "slow": [20], "qty": 100}),
            "train_ratio": "0.6",
            "min_test_trades": "0",
            "rank_by": "total_pnl",
            "commission_per_trade": "0",
        },
    )
    assert r.status_code == 200
    assert "結果" in r.text

    r = client.get("/optimizations")
    assert r.status_code == 200
    run_ids = re.findall(r"/optimizations/(\d+)", r.text)
    assert run_ids

    r = client.get(f"/optimizations/{run_ids[0]}")
    assert r.status_code == 200
    assert "戦略登録" in r.text


def test_performance_page(client):
    r = client.get("/performance")
    assert r.status_code == 200
    assert "成績" in r.text


def test_data_fetch_from_web(client):
    from unittest.mock import patch

    import pandas as pd

    idx = pd.date_range("2026-01-01", periods=4, freq="5min", tz="UTC")
    fake = pd.DataFrame(
        {"Open": [1.0] * 4, "High": [1.0] * 4, "Low": [1.0] * 4, "Close": [1.0] * 4, "Volume": [1] * 4},
        index=idx,
    )
    with patch("app.history.yf.download", return_value=fake):
        r = client.post("/data/fetch", data={"codes": "9005, 9006", "interval": "5m", "period": "60d"})
    assert r.status_code == 200
    assert "取得結果" in r.text
    assert "9005" in r.text and "9006" in r.text

    r = client.get("/data")
    assert r.status_code == 200
    assert "9005" in r.text  # カバレッジ表に反映されている


def test_data_fetch_reports_failure(client):
    from unittest.mock import patch

    import pandas as pd

    with patch("app.history.yf.download", return_value=pd.DataFrame()):
        r = client.post("/data/fetch", data={"codes": "9999", "interval": "5m", "period": "60d"})
    assert r.status_code == 200
    assert "失敗" in r.text
    assert "データ取得できず" in r.text


def test_help_page(client):
    r = client.get("/help")
    assert r.status_code == 200
    assert "ヘルプ" in r.text
    assert "run_all.ps1" in r.text
    assert "fetch_history.py" in r.text


def test_healthz(client):
    assert client.get("/healthz").json() == {"ok": True}


def test_ingest(client):
    r = client.post("/api/ingest", json={"quotes": [{"code": "7203", "price": 2810.0, "volume": 100}]})
    assert r.json()["received"] == 1


def test_ingest_preopen_bid_ask_only(client):
    # 寄り付き前: 現在値なしでも気配があれば受け取る
    r = client.post(
        "/api/ingest",
        json={"quotes": [{"code": "7203", "price": 0, "bid": 2805.0, "ask": 2806.0}]},
    )
    assert r.json()["received"] == 1
    # 完全に空なら弾く
    r = client.post("/api/ingest", json={"quotes": [{"code": "7203"}]})
    assert r.json()["received"] == 0
