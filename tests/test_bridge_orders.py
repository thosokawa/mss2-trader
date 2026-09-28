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
        "発注ロック中（発注を行うには発注機能を有効にしてください）",
        "入力エラー: 数量が不正です",
    ):
        assert bridge._classify_order_status(text) == "rejected"


def test_order_relay_row_is_derived_from_order_id():
    relay = bridge.OrderRelay("dummy.xlsx", n_rows=3)
    # 発注ID 1,2,3 → 2,3,4 行目、4 で一周して 2 行目（bridge を再起動しても同じ）
    assert [relay.row_for(i) for i in range(1, 6)] == [2, 3, 4, 2, 3]
    assert bridge.OrderRelay("dummy.xlsx").row_for(2) == 3


# 実機で返ってきた形（数式が前に付く）
PREFIX = "=@RssStockOrder(A2,B2,C2,D2,E2,F2,G2,H2,I2,J2,K2,L2,M2,N2,O2,P2,Q2,R2,S2,T2) => "


def test_classify_strips_formula_prefix():
    assert bridge._classify_order_status(PREFIX + "発注済み(発注ID=5)", 5) == "sent"
    assert bridge._classify_order_status(
        PREFIX + "発注ロック中(発注を行うには発注機能を有効にしてください)"
    ) == "rejected"
    assert bridge._classify_order_status(PREFIX + "待機中") == "waiting"


def test_classify_sent_requires_matching_order_id():
    # 前の注文（ID=4）の表示が残っているだけなら、まだ確定していない
    assert bridge._classify_order_status("発注済み(発注ID=4)", 5) == "waiting"
    assert bridge._classify_order_status("発注済み(発注ID=5)", 5) == "sent"


def test_classify_order_id_already_used_is_error():
    assert bridge._classify_order_status("発注ID=5 は既に使用済みです。", 5) == "error"


class _FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, sec):
        self.t += sec


def _wait(texts, order_id=5, timeout=15.0):
    relay = bridge.OrderRelay("dummy.xlsx")
    clock = _FakeClock()
    seq = iter(texts)
    last = [None]

    def read():
        last[0] = next(seq, last[0])
        return last[0]

    return relay._wait_result(read, order_id, timeout, clock=clock, sleep=clock.sleep)


def test_wait_result_ignores_stale_sent_of_previous_order():
    res = _wait(["発注済み(発注ID=4)", "発注済み(発注ID=4)", "待機中", PREFIX + "発注済み(発注ID=5)"])
    assert res["status"] == "sent"
    assert "発注ID=5" in res["broker_order_id"]


def test_wait_result_rejected_only_after_settle():
    # 一瞬だけ拒否系の表示 → その後受理された場合は受理が正
    res = _wait(["入力エラー", "発注済み(発注ID=5)"])
    assert res["status"] == "sent"
    # 同じ拒否表示が続けば rejected
    res = _wait([PREFIX + "発注ロック中(発注を行うには発注機能を有効にしてください)"])
    assert res["status"] == "rejected"
    assert "発注ロック中" in res["error"]


def test_wait_result_timeout_when_never_resolved():
    res = _wait(["待機中"], timeout=3.0)
    assert res["status"] == "timeout"


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
