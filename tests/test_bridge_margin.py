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

    with patch.object(relay, "_book", return_value=_FakeBook()), patch.object(relay, "_fire", fake_fire), \
            patch.object(relay, "fresh_positions", return_value=[]), patch.object(bridge.time, "sleep"):
        res = relay.place(_order(id=7, side="SHORT", open_date=0), 1.0)
    assert res["status"] == "sent"
    assert "avg_price" not in res  # 建玉一覧に現れなければ約定単価は報告しない（backend は概算）
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
            patch.object(relay, "fresh_positions", return_value=bridge.parse_positions(REAL)), \
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
            patch.object(relay, "fresh_positions", return_value=[]), \
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


def _lot(price, qty, market=5, date=20260929, side="買建", code="5401", mt=4):
    return {"code": code, "account": "0", "market": market, "margin_type": mt, "side": side,
            "qty": qty, "ordered": 0, "price": price, "date": date}


def test_fill_from_diff_weighted_average_of_new_lots():
    order = _order(side="BUY", open_date=0)
    before = [_lot(688.4, 100)]  # 同じ日に既にあった建玉
    after = [_lot(688.4, 100), _lot(690.0, 100, market=1), _lot(691.0, 100, market=5)]
    qty, avg = bridge.fill_from_diff(before, after, order, 20260929)
    assert qty == 200 and avg == 690.5
    # 同じ値段の建玉に数量が足された場合も増分で数える
    qty, avg = bridge.fill_from_diff([_lot(688.4, 100)], [_lot(688.4, 300)], order, 20260929)
    assert (qty, avg) == (200, 688.4)
    # 別の日・別の売買・別の信用区分は数えない
    others = [_lot(700.0, 100, date=20260928), _lot(700.0, 100, side="売建"), _lot(700.0, 100, mt=2)]
    assert bridge.fill_from_diff([], others, order, 20260929) == (0, None)


def test_margin_open_reports_actual_fill_price():
    relay = bridge.OrderRelay("dummy.xlsx")
    snapshots = iter([[_lot(688.4, 100)], [], [_lot(688.4, 100), _lot(689.5, 100)]])

    def fake_fire(ws, sheet, order_id, values, timeout):
        return {"status": "sent", "broker_order_id": f"発注済み(注文ID={order_id})"}

    with patch.object(relay, "_book", return_value=_FakeBook()), patch.object(relay, "_fire", fake_fire), \
            patch.object(relay, "fresh_positions", side_effect=lambda book: next(snapshots)), \
            patch.object(bridge, "today_yyyymmdd", return_value=20260929), patch.object(bridge.time, "sleep"):
        res = relay.place(_order(id=8, side="BUY", open_date=0), 1.0)
    assert res["status"] == "sent" and res["filled_qty"] == 100 and res["avg_price"] == 689.5


def test_margin_close_errors_when_positions_cannot_be_refreshed():
    relay = bridge.OrderRelay("dummy.xlsx")
    with patch.object(relay, "_book", return_value=_FakeBook()), \
            patch.object(relay, "fresh_positions", return_value=None), patch.object(relay, "_fire") as fire:
        res = relay.place(_order(), 1.0)
    fire.assert_not_called()
    assert res["status"] == "error" and "取り直せなかった" in res["error"]


class _Cell:
    def __init__(self, sheet, addr):
        self.sheet, self.addr = sheet, addr

    @property
    def value(self):
        return self.sheet.values.get(self.addr)

    @value.setter
    def value(self, v):
        self.sheet.values[self.addr] = v

    @property
    def formula(self):
        return self.sheet.formulas.get(self.addr, "")

    @formula.setter
    def formula(self, f):
        self.sheet.formulas[self.addr] = f
        self.sheet.values[self.addr] = f + " => 応答待ち"
        self.sheet.set_count += 1

    def clear_contents(self):
        self.sheet.formulas.pop(self.addr, None)
        self.sheet.values.pop(self.addr, None)


class _PosSheet:
    def __init__(self):
        self.values, self.formulas, self.set_count = {}, {}, 0

    def range(self, addr):
        return _Cell(self, addr)


def test_refresh_positions_reenters_formula_and_waits_for_delivery():
    relay = bridge.OrderRelay("dummy.xlsx")
    ws = _PosSheet()
    book = type("B", (), {"sheets": {bridge.POSITIONS_SHEET: ws}})()
    clock = [0.0]

    def sleep(sec):
        clock[0] += sec
        if clock[0] > 1.5:  # 入れ直して少し経つと配信が始まる
            ws.values["A1"] = ws.formulas["A1"] + " => 配信中"

    assert relay.refresh_positions(book, clock=lambda: clock[0], sleep=sleep)
    assert ws.set_count == 1 and ws.formulas["A1"].startswith("=RssMarginPositionList($A$2:")

    # いつまでも応答待ちなら False（返済しない → error で発注許可 OFF）
    ws2 = _PosSheet()
    book2 = type("B", (), {"sheets": {bridge.POSITIONS_SHEET: ws2}})()
    clock2 = [0.0]
    assert not relay.refresh_positions(book2, timeout=3, clock=lambda: clock2[0],
                                       sleep=lambda s: clock2.__setitem__(0, clock2[0] + s))


def test_read_positions_skips_formula_row():
    relay = bridge.OrderRelay("dummy.xlsx")
    used = type("U", (), {"value": [["=RssMarginPositionList(...) => 配信中"] + [None] * 10, *REAL]})()
    sheet = type("S", (), {"used_range": used})()
    book = type("B", (), {"sheets": {bridge.POSITIONS_SHEET: sheet}})()
    lots = relay.read_positions(book)
    assert len(lots) == 4 and lots[2]["code"] == "5401"
