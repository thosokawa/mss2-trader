"""bridge/bridge.py の発注リレー（P4）のうち、Excel(xlwings) に触れない部分だけを検証する。
実際の RssStockOrder 書き込み・トリガー・セル確定待ちは Windows 実機でしか検証できない。

bridge/ は通常のパッケージではない（bridge.py は `python bridge/bridge.py` として直接実行
される想定）ので、ファイルパスから importlib で読み込む。
"""
import importlib.util
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.db import engine
from app.engine import orders as orders_engine
from app.main import app
from app.models import Order, Strategy

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("bridge_module_under_test", ROOT / "bridge" / "bridge.py")
bridge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bridge)


def test_classify_status_sent():
    assert bridge._classify_order_status("発注済み(発注ID=5)") == "sent"


def test_classify_status_waiting_variants():
    for text in ("待機中", "接続待ち", "応答待ち", "", None):
        assert bridge._classify_order_status(text) == "waiting"


def test_classify_status_cancelled():
    assert bridge._classify_order_status("キャンセル") == "cancelled"


def test_classify_status_rejected_variants():
    for text in (
        "発注ID=5 は既に使用済みです。",
        "発注ロック中（発注を行うには発注機能を有効にしてください）",
        "入力エラー: 数量が不正です",
    ):
        assert bridge._classify_order_status(text) == "rejected"


def test_order_relay_row_wraps_around():
    relay = bridge.OrderRelay("dummy.xlsx", n_rows=3)
    # 2,3,4 を使い切ったら 2 に戻る
    assert [relay._take_row() for _ in range(5)] == [2, 3, 4, 2, 3]


def test_run_order_relay_places_and_reports():
    with TestClient(app) as client:
        with Session(engine) as s:
            st = Strategy(
                name="bridge-relay-test",
                class_path="app.strategy.examples.sma_cross:SmaCross",
                mode="live",
            )
            s.add(st)
            s.commit()
            s.refresh(st)
            placed = orders_engine.queue_order(s, st, "8888", "BUY", 100, "テスト")

        relay = bridge.OrderRelay("dummy.xlsx")
        fake_result = {"status": "sent", "broker_order_id": "発注済み(発注ID=1)"}
        with patch.object(relay, "place", return_value=fake_result):
            n = bridge.run_order_relay("/api/orders", relay, 15.0, client)
        assert n == 1

        r = client.get("/api/orders/pending")
        assert r.json()["orders"] == []  # 既に claim 済みなので pending には出ない

        with Session(engine) as s:
            o = s.get(Order, placed.id)
            assert o.status == "sent"
            assert o.broker_order_id == "発注済み(発注ID=1)"
