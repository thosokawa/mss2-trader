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


def test_watch_symbols_add_and_remove(client):
    r = client.post("/live/watch", data={"codes": "7203, ６５０１"}, follow_redirects=True)
    assert r.status_code == 200
    assert "/live/watch/7203/delete" in r.text and "/live/watch/6501/delete" in r.text
    codes = client.get("/api/quote-codes").json()["codes"]
    assert "7203" in codes and "6501" in codes

    r = client.post("/live/watch/6501/delete", follow_redirects=True)
    assert "/live/watch/6501/delete" not in r.text
    assert "6501" not in client.get("/api/quote-codes").json()["codes"]


def test_symbol_sets_page_is_gone(client):
    assert client.get("/symbol-sets").status_code == 404
    assert "銘柄セット" not in client.get("/").text


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

    client.post(
        "/strategies",
        data={
            "name": "SMAテスト戦略",
            "class_path": "app.strategy.examples.sma_cross:SmaCross",
            "symbols": "7974、８０３５",  # 読点・全角も受け付ける
            "timeframe": "5m",
            "params_json": '{"fast": 5, "slow": 20, "qty": 100}',
            "mode": "notify",
        },
        follow_redirects=True,
    )
    r = client.get("/strategies")
    assert "SMAテスト戦略" in r.text
    assert "停止" in r.text  # 既定は無効
    assert 'value="7974,8035"' in r.text

    strategy_id = max(int(x) for x in re.findall(r"/strategies/(\d+)/toggle", r.text))
    # 無効の間は株価の取り込み対象に入らない
    assert "8035" not in client.get("/api/quote-codes").json()["codes"]
    r = client.post(f"/strategies/{strategy_id}/toggle", follow_redirects=True)
    assert "稼働中" in r.text
    assert "8035" in client.get("/api/quote-codes").json()["codes"]

    r = client.post(f"/strategies/{strategy_id}/symbols", data={"symbols": "7974"}, follow_redirects=True)
    assert 'value="7974"' in r.text
    assert "8035" not in client.get("/api/quote-codes").json()["codes"]

    r = client.post(f"/strategies/{strategy_id}/delete", follow_redirects=True)
    assert "SMAテスト戦略" not in r.text


def test_strategy_duplicate_name_shows_error(client):
    data = {
        "name": "重複名テスト戦略",
        "class_path": "app.strategy.examples.sma_cross:SmaCross",
        "symbols": "7203",
        "timeframe": "5m",
        "params_json": "{}",
        "mode": "notify",
    }
    client.post("/strategies", data=data)
    r = client.post("/strategies", data=data, follow_redirects=True)
    assert r.status_code == 200
    assert "同じ名前の戦略が既にあります" in r.text

    from sqlmodel import Session, select

    from app.db import engine
    from app.models import Strategy

    with Session(engine) as s:
        rows = s.exec(select(Strategy).where(Strategy.name == "重複名テスト戦略")).all()
        assert len(rows) == 1
        for st in rows:
            s.delete(st)
        s.commit()


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


def test_optimize_param_form_allows_ranges(client):
    r = client.get("/optimize")
    assert r.status_code == 200
    assert "allowRanges: true" in r.text
    assert "カンマ区切りで複数指定" in r.text


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
    assert "用語集" in r.text
    assert "プロフィットファクター" in r.text  # glossary が render されている


def test_backtest_with_stop_loss_via_web(client):
    from datetime import datetime, timedelta

    from sqlmodel import Session

    from app.db import engine
    from app.models import Bar, Symbol

    decline = [100 - i for i in range(30)]
    rise = [70 + i * 2 for i in range(7)]
    closes = decline + rise + [70.0, 70.0, 70.0]
    with Session(engine) as s:
        s.add(Symbol(code="8002", name="テスト銘柄"))
        base = datetime(2026, 7, 1)
        for i, price in enumerate(closes):
            s.add(
                Bar(
                    symbol_code="8002", timeframe="5m", ts=base + timedelta(minutes=5 * i),
                    open=price, high=price, low=price, close=price, volume=100.0, source="test",
                )
            )
        s.commit()

    r = client.post(
        "/backtest",
        data={
            "class_path": "app.strategy.examples.sma_cross:SmaCross",
            "symbol_code": "8002",
            "timeframe": "5m",
            "params_json": '{"fast": 5, "slow": 20, "qty": 100, "stop_loss_pct": 3.0}',
            "commission_per_trade": "0",
        },
    )
    assert r.status_code == 200
    assert "損切り" in r.text
    assert "79.5" in r.text  # 82.0 * 0.97 の仕切値


def test_backtest_result_has_glossary_tooltips(client):
    from datetime import datetime, timedelta

    from sqlmodel import Session

    from app.db import engine
    from app.models import Bar, Symbol

    closes = [100 - i for i in range(30)] + [70 + i * 2 for i in range(20)] + [110 - i * 2 for i in range(20)]
    with Session(engine) as s:
        s.add(Symbol(code="8001", name="テスト銘柄"))
        base = datetime(2026, 6, 1)
        for i, price in enumerate(closes):
            s.add(
                Bar(
                    symbol_code="8001", timeframe="5m", ts=base + timedelta(minutes=5 * i),
                    open=price, high=price, low=price, close=price, volume=100.0, source="test",
                )
            )
        s.commit()

    r = client.post(
        "/backtest",
        data={
            "class_path": "app.strategy.examples.sma_cross:SmaCross",
            "symbol_code": "8001",
            "timeframe": "5m",
            "params_json": '{"fast": 5, "slow": 20, "qty": 100}',
            "commission_per_trade": "0",
        },
    )
    assert r.status_code == 200
    assert "プロフィットファクター" in r.text  # PF の title
    assert "最大ドローダウン" in r.text  # 最大DD の title


def test_stop_loss_take_profit_appear_in_all_param_forms(client):
    import json

    escaped_stop = json.dumps("損切り(%)")[1:-1]
    escaped_tp = json.dumps("利確(%)")[1:-1]
    for path in ("/backtest", "/strategies", "/optimize"):
        r = client.get(path)
        assert r.status_code == 200
        assert escaped_stop in r.text, f"{path} に損切りの universal param が無い"
        assert escaped_tp in r.text, f"{path} に利確の universal param が無い"


def test_backtest_param_form_uses_strategy_meta(client):
    import json

    r = client.get("/backtest")
    assert r.status_code == 200
    assert 'id="param_fields"' in r.text
    assert "ParamForm.mount" in r.text
    # SmaCross の param_meta ラベルが JS データに埋め込まれている（tojson は日本語を \uXXXX で出す）
    escaped_label = json.dumps("短期SMA期間")
    assert escaped_label[1:-1] in r.text  # 前後の " を除いた本体部分


def test_risk_page_and_arm_disarm(client):
    r = client.get("/risk")
    assert r.status_code == 200
    assert "ARMED" in r.text

    r = client.post("/risk/arm", follow_redirects=True)
    assert "ARM 中（発注する）" in r.text

    r = client.post("/risk/disarm", data={"reason": "テスト停止"}, follow_redirects=True)
    assert "テスト停止" in r.text
    assert "DISARM（発注しない）" in r.text


def test_status_bar_and_dashboard(client):
    """全ページ共通のステータスバーと、自動更新（autorefresh）の付いたダッシュボード。"""
    r = client.get("/partials/status")
    assert r.status_code == 200
    assert "自動売買" in r.text and "本日の発注" in r.text and "本日の損益" in r.text

    client.post("/risk/arm")
    assert "ARM 中" in client.get("/partials/status").text or "稼働中" in client.get("/partials/status").text
    client.post("/risk/disarm", data={"reason": "テスト"})
    assert "停止理由: テスト" in client.get("/partials/status").text

    r = client.get("/")
    assert r.status_code == 200
    assert 'hx-get="/partials/status"' in r.text  # ステータスバーは全ページ
    assert 'id="dash"' in r.text and 'hx-select="#dash"' in r.text  # ダッシュボードは自動更新
    assert "稼働中の戦略" in r.text and "本日の発注" in r.text
    # メニューは「自動売買」（旧リスク管理）
    assert '>自動売買</a>' in r.text and "リスク管理" not in r.text


def test_autorefresh_regions_on_pages(client):
    for path, region in (("/risk", "risk-live"), ("/strategies", "strat-list"),
                         ("/signals", "signals-list"), ("/performance", "perf"),
                         ("/live", "live-codes"), ("/data", "data-coverage")):
        r = client.get(path)
        assert r.status_code == 200, path
        assert f'id="{region}"' in r.text and f'hx-select="#{region}"' in r.text, path


def test_market_phase():
    from datetime import datetime

    from app.web.status import market_phase

    w = ["09:00-11:30", "12:30-15:30"]
    # 2026-09-29(火) JST → UTC は -9h
    assert market_phase(datetime(2026, 9, 29, 0, 30), w) == ("取引時間中", True)  # 09:30
    assert market_phase(datetime(2026, 9, 29, 3, 0), w) == ("昼休み", False)  # 12:00
    assert market_phase(datetime(2026, 9, 28, 23, 30), w) == ("寄り前", False)  # 08:30
    assert market_phase(datetime(2026, 9, 29, 7, 0), w) == ("取引時間外", False)  # 16:00
    assert market_phase(datetime(2026, 10, 3, 1, 0), w) == ("休日", False)  # 土曜


def test_deleted_strategy_id_is_not_reused(client):
    """削除は論理削除。新しい戦略に旧戦略の id（＝注文・シグナル）が引き継がれない。"""
    import re

    from sqlmodel import Session, select

    from app.db import engine
    from app.models import Strategy

    data = {"class_path": "app.strategy.examples.sma_cross:SmaCross", "symbols": "7203",
            "timeframe": "5m", "params_json": "{}", "mode": "notify"}
    client.post("/strategies", data={**data, "name": "消す戦略"})
    r = client.get("/strategies")
    old_id = max(int(x) for x in re.findall(r"/strategies/(\d+)/delete", r.text))
    client.post(f"/strategies/{old_id}/delete")
    r = client.post("/strategies", data={**data, "name": "消す戦略"}, follow_redirects=True)
    assert "同じ名前" not in r.text  # 同じ名前で作り直せる
    new_id = max(int(x) for x in re.findall(r"/strategies/(\d+)/delete", r.text))
    assert new_id != old_id
    with Session(engine) as s:
        old = s.get(Strategy, old_id)
        assert old.deleted and not old.enabled and "削除済み" in old.name
        assert f"/strategies/{old_id}/" not in r.text  # 一覧には出ない
        assert s.exec(select(Strategy).where(Strategy.name == "消す戦略")).one().id == new_id


def test_orders_pending_and_report_flow(client):
    from sqlmodel import Session

    from app.db import engine
    from app.engine import orders as orders_engine
    from app.models import Order, Strategy

    with Session(engine) as s:
        st = Strategy(
            name="live-web-test", class_path="app.strategy.examples.sma_cross:SmaCross", mode="live"
        )
        s.add(st)
        s.commit()
        s.refresh(st)
        placed = orders_engine.queue_order(s, st, "7777", "BUY", 100, "テスト")
        order_id = placed.id

    r = client.get("/api/orders/pending")
    assert r.status_code == 200
    ids = [o["id"] for o in r.json()["orders"]]
    assert order_id in ids

    r = client.post(
        f"/api/orders/{order_id}/report",
        json={"status": "filled", "broker_order_id": "ORD1", "filled_qty": 100, "avg_price": 2500.0},
    )
    assert r.status_code == 200 and r.json()["ok"] is True

    with Session(engine) as s:
        o = s.get(Order, order_id)
        assert o.status == "filled" and o.broker_order_id == "ORD1" and o.avg_price == 2500.0

    r = client.post(f"/api/orders/{order_id}/report", json={"status": "filled"})
    assert r.status_code == 200  # 既知の注文なら再報告も許容


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
