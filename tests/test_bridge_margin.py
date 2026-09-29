"""bridge の信用注文（RssMarginOpenOrder / RssMarginCloseOrder）のうち Excel に触れない部分。

建玉一覧の値は 2026-09-28 に実機（--probe-account の positions シート）で取得したものを使う。
"""
from unittest.mock import patch

from tests.test_bridge_orders import bridge

HEADER = ["銘柄コード", "銘柄名称", "口座区分", "建市場", "信用区分", "弁済期限", "売買", "建玉数量",
          "発注数量", "建値", "建日"]
END = ["--------"] * len(HEADER)

# 実機の値（抜粋。手動で建てた建玉が混ざっている）
REAL = [
    HEADER,
    [1540.0, "純金上場信託", "特定", "東証", "一般", "無期限", "買建", 5.0, 0.0, 24605.0, 20260227.0],
    [1540.0, "純金上場信託", "特定", "JAX", "一般", "無期限", "買建", 5.0, 0.0, 21899.0, 20260325.0],
    [5401.0, "日本製鉄", "特定", "JAX", "一般", "1日", "買建", 100.0, 0.0, 688.4, 20260928.0],
    [7974.0, "任天堂", "特定", "JAX", "一般", "無期限", "買建", 100.0, 0.0, 9016.9, 20260831.0],
    END,
]


def _order(**kw):
    o = {"id": 42, "symbol_code": "5401", "side": "EXIT", "qty": 100, "trade_type": "margin",
         "margin_type": 4, "account_type": "0", "open_date": 20260928}
    o.update(kw)
    return o


def test_parse_positions_real_values():
    lots = bridge.parse_positions(REAL)
    assert len(lots) == 4  # '--------' で止まる
    nippon = lots[2]
    assert nippon == {
        "code": "5401", "account": "0", "market": 5, "margin_type": 4, "side": "買建",
        "qty": 100, "ordered": 0, "price": 688.4, "date": 20260928,
    }
    assert lots[0]["market"] == 1 and lots[0]["margin_type"] == 2


def test_parse_positions_waiting_or_empty():
    waiting = [HEADER, ["=@RssMarginPositionList($A$1:$K$1) => 応答待ち"] + [None] * 10]
    assert bridge.parse_positions(waiting) == []
    assert bridge.parse_positions([HEADER, END]) == []
    assert bridge.parse_positions([]) == []


def test_parse_positions_seido_and_short():
    table = [HEADER,
             [9984.0, "SBG", "一般", "東証", "制度", "6ヶ月", "売建", 200.0, 100.0, 6300.0, 20260901.0]]
    lot = bridge.parse_positions(table)[0]
    assert (lot["margin_type"], lot["side"], lot["account"], lot["ordered"]) == (1, "売建", "1", 100)


def test_select_lots_picks_only_bot_position():
    picked, why = bridge.select_lots(bridge.parse_positions(REAL), _order())
    assert why == ""
    assert [(lot["code"], lot["date"], q) for lot, q in picked] == [("5401", 20260928, 100)]


def test_select_lots_ignores_manual_positions_of_other_day_or_type():
    lots = bridge.parse_positions(REAL)
    # 任天堂は無期限・8/31 建て。bot が今日いちにちで建てた前提なら該当なし
    picked, why = bridge.select_lots(lots, _order(symbol_code="7974"))
    assert picked == [] and "足りない" in why
    # 信用区分が違えば該当なし（5401 は いちにち=4 の建玉しかない）
    picked, _ = bridge.select_lots(lots, _order(margin_type=2))
    assert picked == []
    # 売建の買戻し（COVER）は買建には当てない
    picked, _ = bridge.select_lots(lots, _order(side="COVER"))
    assert picked == []


def test_select_lots_splits_across_lots_and_respects_pending_close():
    table = [
        HEADER,
        [5401.0, "日本製鉄", "特定", "東証", "一般", "1日", "買建", 100.0, 0.0, 688.4, 20260928.0],
        [5401.0, "日本製鉄", "特定", "JAX", "一般", "1日", "買建", 100.0, 0.0, 688.5, 20260928.0],
        [5401.0, "日本製鉄", "特定", "東証", "一般", "1日", "買建", 100.0, 100.0, 688.6, 20260928.0],
        END,
    ]
    picked, why = bridge.select_lots(bridge.parse_positions(table), _order(qty=200))
    assert why == ""
    assert sorted(q for _, q in picked) == [100, 100]
    assert all(lot["price"] != 688.6 for lot, _ in picked)  # 返済注文中の建玉は使わない
    # 300 株は足りない
    picked, why = bridge.select_lots(bridge.parse_positions(table), _order(qty=300))
    assert picked == [] and "必要300株" in why


def test_combine_close_results():
    ok = {"status": "sent", "broker_order_id": "発注済み(発注ID=1)"}
    ng = {"status": "rejected", "error": "入力エラー"}
    assert bridge.combine_close_results([ok, ok], 2)["status"] == "sent"
    assert bridge.combine_close_results([ng], 2) == ng  # 1件も出ていない＝確実に未発注
    partial = bridge.combine_close_results([ok, ng], 2)
    assert partial["status"] == "error" and "一部" in partial["error"]


class _FakeSheet:
    name = "x"


class _FakeBook:
    sheets = {"margin_open": _FakeSheet(), "margin_close": _FakeSheet()}


def test_margin_open_writes_22_args():
    relay = bridge.OrderRelay("dummy.xlsx")
    fired = {}

    def fake_fire(ws, sheet, order_id, values, timeout):
        fired.update(sheet=sheet, order_id=order_id, values=values)
        return {"status": "sent", "broker_order_id": f"発注済み(発注ID={order_id})"}

    with patch.object(relay, "_book", return_value=_FakeBook()), patch.object(relay, "_fire", fake_fire):
        res = relay.place(_order(id=7, side="SHORT", open_date=0), 1.0)
    assert res["status"] == "sent"
    v = fired["values"]
    assert fired["sheet"] == "margin_open" and len(v) == 22
    # 発注ID, トリガー, 銘柄, 売買区分(1=売建), 注文区分, SOR(1=既定), 信用区分(4=いちにち), 数量, 価格区分
    assert v[:9] == [7, 0, "5401", 1, 0, 1, 4, 100, 0]
    assert v[10] == 1 and v[12] == "0"  # 執行条件=本日中, 口座区分=特定


def test_margin_close_uses_position_date_price_market():
    relay = bridge.OrderRelay("dummy.xlsx")
    fired = []

    def fake_fire(ws, sheet, order_id, values, timeout):
        fired.append((sheet, order_id, values))
        return {"status": "sent", "broker_order_id": f"発注済み(発注ID={order_id})"}

    with patch.object(relay, "_book", return_value=_FakeBook()), \
            patch.object(relay, "read_positions", return_value=bridge.parse_positions(REAL)), \
            patch.object(relay, "_fire", fake_fire):
        res = relay.place(_order(id=42), 1.0)
    assert res["status"] == "sent"
    (sheet, sub_id, v), = fired
    assert sheet == "margin_close" and len(v) == 20
    assert sub_id == bridge.CLOSE_ID_BASE + 42 * bridge.CLOSE_ID_SLOTS  # Order.id と重ならない
    # 売買区分(1=売埋), 信用区分, 数量, 建日, 建単価, 建市場(5=JAX)
    assert v[3] == 1 and v[5] == 1 and v[6] == 4 and v[7] == 100
    assert v[13:16] == [20260928, 688.4, 5]


def test_margin_close_without_matching_position_is_error():
    relay = bridge.OrderRelay("dummy.xlsx")
    with patch.object(relay, "_book", return_value=_FakeBook()), \
            patch.object(relay, "read_positions", return_value=[]), \
            patch.object(relay, "_fire") as fire:
        res = relay.place(_order(), 1.0)
    fire.assert_not_called()
    assert res["status"] == "error"  # backend が DISARM → 人が確認


def test_close_ids_do_not_collide_with_order_ids_or_rows():
    relay = bridge.OrderRelay("dummy.xlsx")
    sub = bridge.CLOSE_ID_BASE + 5 * bridge.CLOSE_ID_SLOTS
    assert sub <= 2147483647  # RSS の発注ID上限
    assert 2 <= relay.row_for(sub) <= 301


def test_sor_can_be_turned_off_for_stock_orders():
    fired = {}

    def fake_fire(ws, sheet, order_id, values, timeout):
        fired.update(sheet=sheet, values=values)
        return {"status": "sent", "broker_order_id": "発注済み"}

    class _Book:
        sheets = {"orders": _FakeSheet()}

    for sor, want in ((1, 1), (0, 0)):
        relay = bridge.OrderRelay("dummy.xlsx", sor=sor)
        with patch.object(relay, "_book", return_value=_Book()), patch.object(relay, "_fire", fake_fire):
            order = {"id": 3, "symbol_code": "9984", "side": "BUY", "qty": 100, "trade_type": "cash"}
            relay.place(order, 1.0)
        assert fired["sheet"] == "orders" and fired["values"][5] == want
