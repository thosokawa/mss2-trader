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
    r = client.post("/symbols/watch", data={"codes": "7203, ６５０１"}, follow_redirects=True)
    assert r.status_code == 200
    assert "/symbols/watch/7203/delete" in r.text and "/symbols/watch/6501/delete" in r.text
    codes = client.get("/api/quote-codes").json()["codes"]
    assert "7203" in codes and "6501" in codes

    r = client.post("/symbols/watch/6501/delete", follow_redirects=True)
    assert "/symbols/watch/6501/delete" not in r.text
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
    # 一覧は JSON ではなく要約文
    assert "SMAクロス（5/20）" in r.text and "&#34;fast&#34;" not in r.text

    strategy_id = max(int(x) for x in re.findall(r"/strategies/(\d+)/toggle", r.text))
    assert 'value="7974,8035"' in client.get(f"/strategies/{strategy_id}/edit").text
    # 無効の間は株価の取り込み対象に入らない
    assert "8035" not in client.get("/api/quote-codes").json()["codes"]
    r = client.post(f"/strategies/{strategy_id}/toggle", follow_redirects=True)
    assert "稼働中" in r.text
    assert "8035" in client.get("/api/quote-codes").json()["codes"]

    r = client.post(f"/strategies/{strategy_id}/symbols", data={"symbols": "7974"}, follow_redirects=True)
    assert 'value="7974"' in client.get(f"/strategies/{strategy_id}/edit").text
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
    for path in ("/backtest", "/strategies/new", "/optimize"):
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
    assert "発注許可" in r.text

    r = client.post("/risk/arm", follow_redirects=True)
    assert "ON — 発注する" in r.text

    r = client.post("/risk/disarm", data={"reason": "テスト停止"}, follow_redirects=True)
    assert "テスト停止" in r.text
    assert "OFF — 発注しない" in r.text


def test_status_bar_and_dashboard(client):
    """全ページ共通のステータスバーと、自動更新（autorefresh）の付いたダッシュボード。"""
    r = client.get("/partials/status")
    assert r.status_code == 200
    assert "自動売買" in r.text and "本日の発注" in r.text and "本日の損益" in r.text

    client.post("/risk/arm")
    assert "発注許可 ON" in client.get("/partials/status").text
    client.post("/risk/disarm", data={"reason": "テスト"})
    assert "停止理由: テスト" in client.get("/partials/status").text

    r = client.get("/")
    assert r.status_code == 200
    assert 'hx-get="/partials/status"' in r.text  # ステータスバーは全ページ
    assert 'id="dash"' in r.text and 'hx-select="#dash"' in r.text  # ダッシュボードは自動更新
    assert "稼働中の戦略" in r.text and "本日の発注" in r.text
    # メニュー: ダッシュボード / 自動売買 / 銘柄・データ / バックテスト / 最適化 / ヘルプ
    for label in ("ダッシュボード", "自動売買", "銘柄・データ", "バックテスト", "最適化", "ヘルプ"):
        assert f">{label}</a>" in r.text, label
    assert "リスク管理" not in r.text and ">ライブ</a>" not in r.text


def test_autorefresh_regions_on_pages(client):
    for path, region in (("/risk", "risk-live"),
                         ("/signals", "signals-list"), ("/performance", "perf"),
                         ("/symbols", "symbols-list"), ("/orders", "orders-list"),
                         ("/data", "data-coverage")):
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
    old_id = max(int(x) for x in re.findall(r"/strategies/(\d+)/toggle", r.text))
    client.post(f"/strategies/{old_id}/delete")
    r = client.post("/strategies", data={**data, "name": "消す戦略"}, follow_redirects=True)
    assert "同じ名前" not in r.text  # 同じ名前で作り直せる
    new_id = max(int(x) for x in re.findall(r"/strategies/(\d+)/toggle", client.get("/strategies").text))
    assert new_id != old_id
    with Session(engine) as s:
        old = s.get(Strategy, old_id)
        assert old.deleted and not old.enabled and "削除済み" in old.name
        assert f"/strategies/{old_id}/" not in client.get("/strategies").text  # 一覧には出ない
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


def test_symbols_page_and_tabs(client):
    client.post("/symbols/watch", data={"codes": "7203"})
    client.post("/strategies", data={
        "name": "収集理由テスト", "class_path": "app.strategy.examples.sma_cross:SmaCross",
        "symbols": "7203,6758", "timeframe": "5m", "params_json": "{}", "mode": "live"})
    import re
    sid = max(int(x) for x in re.findall(r"/strategies/(\d+)/toggle", client.get("/strategies").text))
    client.post(f"/strategies/{sid}/toggle")

    r = client.get("/symbols")
    assert "データ収集中の銘柄" in r.text
    assert "戦略: 収集理由テスト" in r.text  # 戦略が使っている銘柄は理由を表示
    assert "/symbols/watch/7203/delete" in r.text  # 監視銘柄は「外す」
    assert "/symbols/watch/6758/delete" not in r.text  # 戦略だけで収集中の銘柄は外せない
    assert client.get("/live", follow_redirects=False).status_code == 301

    # 自動売買のタブ
    for path in ("/risk", "/strategies", "/signals", "/orders", "/performance"):
        html = client.get(path).text
        assert 'class="tabs"' in html and 'href="/orders"' in html, path
        assert 'aria-current="page">自動売買</a>' in html, path
    assert "実発注" in client.get("/strategies").text  # モード名は日本語
    client.post(f"/strategies/{sid}/toggle")


def test_strategy_new_detail_edit_flow(client):
    """追加 → 詳細（日本語の設定表示）→ 編集（パラメータ変更）→ 詳細に反映。"""
    import re

    r = client.post("/strategies", data={
        "name": "編集フロー戦略", "class_path": "app.strategy.examples.macd_cross:MacdCross",
        "symbols": "7203", "timeframe": "1m", "mode": "notify",
        "params_json": '{"fast": 12, "slow": 26, "signal": 9, "qty": 100, "stop_loss_pct": 0.5,'
                       ' "hold_overnight": false, "direction": "both"}',
    }, follow_redirects=True)
    assert r.status_code == 200
    sid = int(re.search(r"/strategies/(\d+)/edit", r.text).group(1))
    # 詳細: パラメータは日本語の項目名
    assert "MACD短期期間" in r.text and "損切り" in r.text and "0.5%" in r.text
    assert "買い・売り" in r.text and "またがない" in r.text and "通知のみ" in r.text

    # 編集フォームは既存値で埋まっている
    r = client.get(f"/strategies/{sid}/edit")
    assert 'value="編集フロー戦略"' in r.text and "&#34;stop_loss_pct&#34;: 0.5" in r.text

    # パラメータ・名前・モードを変える
    r = client.post(f"/strategies/{sid}/edit", data={
        "name": "編集フロー戦略2", "class_path": "app.strategy.examples.macd_cross:MacdCross",
        "symbols": "7203", "timeframe": "1m", "mode": "paper",
        "params_json": '{"fast": 10, "slow": 30, "signal": 9, "qty": 200, "stop_loss_pct": 1}',
    }, follow_redirects=True)
    assert "編集フロー戦略2" in r.text and "ペーパー" in r.text and "200 株" in r.text and "1%" in r.text

    # 名前の重複はフォームにエラーを出して入力を保つ
    client.post("/strategies", data={"name": "別の戦略", "symbols": "7203", "params_json": "{}",
                                     "class_path": "app.strategy.examples.sma_cross:SmaCross"})
    r = client.post(f"/strategies/{sid}/edit", data={
        "name": "別の戦略", "class_path": "app.strategy.examples.macd_cross:MacdCross",
        "symbols": "7203", "timeframe": "1m", "mode": "paper", "params_json": "{}"})
    assert "同じ名前" in r.text and 'value="別の戦略"' in r.text


def test_strategy_edit_is_locked_while_live_position_open(client):
    import re

    from sqlmodel import Session

    from app.db import engine
    from app.engine import orders
    from app.models import Strategy

    r = client.post("/strategies", data={
        "name": "建玉ロック戦略", "class_path": "app.strategy.examples.sma_cross:SmaCross",
        "symbols": "9931,9932", "timeframe": "1m", "mode": "live", "params_json": '{"qty": 100}',
    }, follow_redirects=True)
    sid = int(re.search(r"/strategies/(\d+)/edit", r.text).group(1))
    with Session(engine) as s:
        st = s.get(Strategy, sid)
        o = orders.queue_order(s, st, "9931", "BUY", 100, "test", ref_price=1000.0)
        o.status = "sent"
        s.add(o)
        s.commit()

    r = client.get(f"/strategies/{sid}/edit")
    assert "変えられません" in r.text
    base = {"name": "建玉ロック戦略", "class_path": "app.strategy.examples.sma_cross:SmaCross",
            "timeframe": "1m", "mode": "live", "params_json": '{"qty": 100, "stop_loss_pct": 2}'}
    # モード変更・建玉のある銘柄の除外は拒否
    r = client.post(f"/strategies/{sid}/edit", data={**base, "symbols": "9931,9932", "mode": "paper"})
    assert "建玉を持っています" in r.text
    r = client.post(f"/strategies/{sid}/edit", data={**base, "symbols": "9932"})
    assert "建玉を持っています" in r.text
    # パラメータ（損切り）の変更と、建玉の無い銘柄の除外は OK
    r = client.post(f"/strategies/{sid}/edit", data={**base, "symbols": "9931"}, follow_redirects=True)
    assert "2%" in r.text
    with Session(engine) as s:
        assert s.get(Strategy, sid).symbols == "9931"
        orders.record_manual_close(s, sid, "9931")


def test_performance_shows_live_round_trips(client):
    from sqlmodel import Session

    from app.db import engine
    from app.engine import orders
    from app.models import Strategy

    with Session(engine) as s:
        st = Strategy(name="成績テスト戦略", class_path="a:B", symbols="9941", mode="live")
        s.add(st)
        s.commit()
        s.refresh(st)
        for side, px in (("BUY", 1000.0), ("EXIT", 1012.5), ("BUY", 1010.0), ("EXIT", 1005.0)):
            o = orders.queue_order(s, st, "9941", side, 100, side, ref_price=px)
            o.status = "sent"
            s.add(o)
        s.commit()
        trips = orders.round_trips(s, st.id)
        assert [t["pnl"] for t in trips] == [1250.0, -500.0]
        m = orders.summarize_trips(trips)
        assert m["realized_pnl"] == 750 and m["win_rate_pct"] == 50.0 and m["profit_factor"] == 2.5

    r = client.get("/performance")
    assert "実発注" in r.text and "成績テスト戦略" in r.text and "▲ +750 円" in r.text
    assert "1,000.0 → 1,012.5" in r.text


def test_signals_filter_by_strategy(client):
    r = client.get("/signals", params={"strategy_id": 999999})
    assert r.status_code == 200 and "すべての戦略" in r.text


def test_trade_type_and_direction_show_japanese_labels(client):
    """取引区分・売買方向の選択肢は、値は cash/margin・long/short/both のまま表示名が日本語。"""
    import json

    from app.strategy.registry import UNIVERSAL_META

    assert UNIVERSAL_META["trade_type"]["choice_labels"] == {"cash": "現物", "margin": "信用"}
    assert UNIVERSAL_META["direction"]["choice_labels"]["long"] == "買いのみ"
    r = client.get("/strategies/new")
    assert json.dumps("信用")[1:-1] in r.text and "choice_labels" in r.text
    js = client.get("/static/param_form.js").text
    assert "m.choice_labels" in js


def test_static_assets_are_cache_busted(client):
    import re

    html = client.get("/strategies/new").text
    assert re.search(r'/static/app\.css\?v=\d+', html)
    assert re.search(r'/static/param_form\.js\?v=\d+', html)
    assert client.get(re.search(r'(/static/param_form\.js\?v=\d+)', html).group(1)).status_code == 200


def test_qty_steps_by_100_and_trade_type_is_colored(client):
    """株数は上下の矢印で 100 ずつ（最小 100）、取引区分は選択中の値で select の色が変わる。"""
    from app.strategy.registry import BUILTIN, builtin_param_meta

    metas = builtin_param_meta()
    for cp in BUILTIN.values():
        assert metas[cp]["qty"]["step"] == 100 and metas[cp]["qty"]["min"] == 100, cp
        assert metas[cp]["qty"]["label"] == "株数"  # 戦略側のラベルは残る
        classes = metas[cp]["trade_type"]["choice_classes"]
        assert classes == {"cash": "pf-choice-cash", "margin": "pf-choice-margin"}
    js = client.get("/static/param_form.js").text
    assert "m.choice_classes" in js and "m.step" in js and "m.min" in js
    css = client.get("/static/app.css").text
    assert ".pf-choice-margin" in css and ".pf-choice-cash" in css


def test_direction_and_mode_are_colored(client):
    """売買方向（買い=赤/売り=青/両方=半々）とモードも、選択中の値で select の色が変わる。"""
    from app.strategy.registry import BUILTIN, builtin_param_meta

    metas = builtin_param_meta()
    for cp in BUILTIN.values():
        assert metas[cp]["direction"]["choice_classes"] == {
            "long": "pf-choice-long", "short": "pf-choice-short", "both": "pf-choice-both"}
    html = client.get("/strategies/new").text
    assert "ParamForm.paintChoices(document.getElementById('mode')" in html
    css = client.get("/static/app.css").text
    for cls in ("long", "short", "both", "notify", "paper", "live"):
        assert f".pf-choice-{cls}" in css


def test_pnl_colors_follow_marketspeed(client):
    """金額はプラス=赤(up)・マイナス=青(dn)。OK/エラー表示の pos/neg とは別のクラス。"""
    from app.web.routes import _pnl_cls

    assert (_pnl_cls(1250), _pnl_cls(-500), _pnl_cls(0), _pnl_cls(None)) == ("up", "dn", "muted", "muted")
    css = client.get("/static/app.css").text
    assert "--c-up: #FF5A5A" in css and ".up { color: var(--c-up); }" in css
    assert "--c-dn: #5AA9FF" in css  # マイナスは青


def test_strategies_are_listed_in_trading_overview(client):
    """旧「戦略」タブは 自動売買 › 概要 に統合。/strategies は概要の戦略の一覧へ転送する。"""
    import re

    client.post("/strategies", data={"name": "概要統合テスト戦略", "symbols": "7203", "params_json": "{}",
                                     "class_path": "app.strategy.examples.sma_cross:SmaCross"})
    r = client.get("/strategies", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/risk#strategies"
    loc = client.get("/strategies?error=dup_name", follow_redirects=False).headers["location"]
    assert loc == "/risk?error=dup_name#strategies"

    html = client.get("/risk").text
    assert 'id="strategies"' in html and "＋ 戦略を追加" in html and "概要統合テスト戦略" in html
    assert 'href="/strategies">戦略</a>' not in html  # タブに「戦略」は無い
    assert 'name="next" value="/risk#strategies"' in html  # 概要の有効化ボタンは概要に戻る

    sid = max(int(x) for x in re.findall(r"/strategies/(\d+)/toggle", html))
    detail = client.get(f"/strategies/{sid}").text
    assert 'href="/risk" aria-current="page">概要</a>' in detail  # 詳細画面では「概要」タブが選択中
    assert 'href="/risk#strategies" class="backlink"' in detail


def test_backtest_history_is_readable(client):
    """履歴は実行日時を JST で、条件は生の JSON でなく日本語の要約・設定表で出す。"""
    import json
    from datetime import datetime

    from sqlmodel import Session

    from app.db import engine
    from app.models import BacktestRun

    params = {"fast_period": 10, "mid_period": 30, "rsi_period": 14, "direction": "both",
              "hold_overnight": False, "qty": 100}
    with Session(engine) as s:
        run = BacktestRun(strategy_name="TrendRsiReclaim",
                          class_path="app.strategy.examples.trend_rsi_reclaim:TrendRsiReclaim",
                          params_json=json.dumps(params), symbol_code="5016", timeframe="5m",
                          start=datetime(2026, 6, 29, 0, 0), end=datetime(2026, 9, 29, 6, 25),
                          metrics_json=json.dumps({"trades": 17, "win_rate_pct": 70.6, "total_pnl": 29600.0,
                                                   "max_drawdown": -27400.0, "profit_factor": 2.02,
                                                   "best": 12400.0, "worst": -11100.0}),
                          created_at=datetime(2026, 9, 29, 6, 30, 57))
        s.add(run)
        s.commit()
        s.refresh(run)
        rid = run.id
    with Session(engine) as s:
        from app.models import SymbolMaster
        if not s.get(SymbolMaster, "5016"):
            s.add(SymbolMaster(code="5016", name="ＪＸ金属"))
            s.commit()
    html = client.get("/backtests").text
    assert "09/29 15:30" in html  # 06:30 UTC → 15:30 JST
    assert "ＪＸ金属" in html  # 銘柄コードだけでなく銘柄名も（銘柄マスタから補完）
    assert "トレンド×RSI出戻り（10/30/14" in html and "デイトレ" in html
    assert "▲ +29,600" in html and "70.6%" in html
    detail = client.get(f"/backtests/{rid}").text
    assert "params {" not in detail and '"mid_period"' not in detail  # 生の JSON は出さない
    assert "中期MA期間" in detail and "買い・売り" in detail and "またがない" in detail
    assert "実行 2026-09-29 15:30" in detail and "ＪＸ金属" in detail


def test_backtest_can_be_registered_as_strategy(client):
    """バックテストの設定（ロジック・パラメータ・銘柄・足・名前）を入れた状態で「戦略を追加」を開ける。"""
    import html as htmllib
    import json
    import re
    from datetime import datetime

    from sqlmodel import Session

    from app.db import engine
    from app.models import BacktestRun

    cp = "app.strategy.examples.trend_rsi_reclaim:TrendRsiReclaim"
    params = {"rsi_period": 9, "rsi_buy_level": 45, "direction": "both", "exit_rule": "sma_cross"}
    with Session(engine) as s:
        run = BacktestRun(strategy_name="TrendRsiReclaim", class_path=cp, params_json=json.dumps(params),
                          symbol_code="5016", timeframe="5m", metrics_json="{}",
                          start=datetime(2026, 6, 29), end=datetime(2026, 9, 29))
        daily = BacktestRun(strategy_name="TrendRsiReclaim", class_path=cp, params_json="{}",
                            symbol_code="5016", timeframe="1d", metrics_json="{}")
        s.add(run)
        s.add(daily)
        s.commit()
        rid, did = run.id, daily.id
    detail = client.get(f"/backtests/{rid}").text
    url = htmllib.unescape(re.search(r'href="(/strategies/new[?][^"]+)"', detail).group(1))
    assert "この設定で自動売買の戦略に登録" in detail
    form = client.get(url).text
    assert cp in form and 'value="5016"' in form and "トレンド×RSI出戻り" in form
    assert '<option selected>5m</option>' in form or "selected>5m" in form
    assert "&#34;rsi_period&#34;: 9" in form or '"rsi_period": 9' in form or "rsi_period&#34;: 9" in form
    # 日足のバックテストは足を入れない（自動売買は 1m/5m/15m）
    ddetail = client.get(f"/backtests/{did}").text
    durl = htmllib.unescape(re.search(r'href="(/strategies/new[?][^"]+)"', ddetail).group(1))
    assert "timeframe" not in durl
    assert "/strategies/new?" in client.get("/backtests").text


def test_indexes_for_latest_rows_exist(client):
    """「銘柄ごとの最新 tick」等を並べ替えなしで取るための複合インデックス（画面の表示速度のため）。"""
    from app.db import engine

    with engine.connect() as conn:
        names = {r[0] for r in conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='index'")}
        plan = conn.exec_driver_sql(
            "EXPLAIN QUERY PLAN SELECT * FROM tick WHERE symbol_code='9984' AND price > 0 "
            "ORDER BY ts DESC LIMIT 1"
        ).fetchall()
    want = {"ix_tick_symbol_ts", "ix_tick_received_at", "ix_bar_symbol_tf_ts", "ix_signal_strategy_ts"}
    assert want <= names
    assert "TEMP B-TREE" not in " ".join(str(r) for r in plan)  # 全件並べ替えをしない
