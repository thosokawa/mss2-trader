import pandas as pd
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import symbols
from app.db import engine, init_db
from app.main import app
from app.models import Strategy, Symbol, SymbolMaster

# JPX の data_j.xlsx と同じ列（2026-08 時点の実物から抜粋）
JPX = pd.DataFrame(
    [
        ["20260831", "1540", "純金上場信託（現物国内保管型）", "ETF・ETN", "-", "-"],
        ["20260831", "130A", "Ｖｅｒｉｔａｓ　Ｉｎ　Ｓｉｌｉｃｏ", "グロース（内国株式）", "3250", "医薬品"],
        ["20260831", "5401", "日本製鉄", "プライム（内国株式）", "3450", "鉄鋼"],
        ["20260831", "6613", "ＱＤレーザ", "グロース（内国株式）", "3650", "電気機器"],
        ["20260831", "9984", "ソフトバンクグループ", "プライム（内国株式）", "5250", "情報・通信業"],
    ],
    columns=["日付", "コード", "銘柄名", "市場・商品区分", "33業種コード", "33業種区分"],
)


def _refresh():
    init_db()
    with Session(engine) as s:
        return symbols.refresh_master(s, df=JPX)


def test_refresh_and_lookup_normalizes_names():
    res = _refresh()
    assert res["count"] == 5 and res["as_of"] == "20260831"
    with Session(engine) as s:
        assert symbols.lookup_name(s, "9984") == "ソフトバンクグループ"
        assert symbols.lookup_name(s, "6613") == "QDレーザ"  # 全角英字 → 半角
        assert symbols.lookup_name(s, "130a") == "Veritas In Silico"  # 小文字コードも可
        assert symbols.lookup_name(s, "９９８４") == "ソフトバンクグループ"  # 全角数字のコードも可
        m = symbols.lookup(s, "1540")
        assert m.market == "ETF・ETN" and m.sector == ""
        assert symbols.lookup_name(s, "0000") == ""


def test_refresh_replaces_previous_master():
    _refresh()
    with Session(engine) as s:
        symbols.refresh_master(s, df=JPX[JPX["コード"] == "9984"])
        assert len(s.exec(select(SymbolMaster)).all()) == 1
    _refresh()  # 他のテストのために戻す


def test_refresh_fills_missing_symbol_names():
    init_db()
    with Session(engine) as s:
        s.add(Symbol(code="5401", name=""))
        s.commit()
        res = symbols.refresh_master(s, df=JPX)
        assert res["filled"] >= 1
        assert s.get(Symbol, "5401").name == "日本製鉄"


def test_api_symbol_name():
    _refresh()
    with TestClient(app) as c:
        assert c.get("/api/symbol-name", params={"code": "9984"}).json() == {
            "code": "9984", "name": "ソフトバンクグループ", "market": "プライム（内国株式）",
        }
        assert c.get("/api/symbol-name", params={"code": "0000"}).json()["name"] == ""


def test_parse_codes():
    assert symbols.parse_codes("9984, 5016 7203") == ["9984", "5016", "7203"]
    assert symbols.parse_codes("９９８４、130a，9984") == ["9984", "130A"]  # 全角・読点・重複
    assert symbols.parse_codes("") == []


def test_strategy_and_watch_register_symbols_with_names():
    _refresh()
    with TestClient(app) as c:
        c.post("/strategies", data={
            "name": "補完テスト戦略", "class_path": "app.strategy.examples.sma_cross:SmaCross",
            "symbols": "6613,130a", "timeframe": "5m", "params_json": "{}", "mode": "notify",
        })
        c.post("/symbols/watch", data={"codes": "1540"})
    with Session(engine) as s:
        assert s.get(Symbol, "6613").name == "QDレーザ"
        assert s.get(Symbol, "130A").name == "Veritas In Silico"  # コードも大文字にそろう
        assert s.get(Symbol, "1540").name == "純金上場信託(現物国内保管型)"
        st = s.exec(select(Strategy).where(Strategy.name == "補完テスト戦略")).one()
        assert st.symbols == "6613,130A"


def test_data_page_shows_master_status():
    _refresh()
    with TestClient(app) as c:
        r = c.get("/data")
        assert "銘柄一覧を更新" in r.text and "取り込み済み" in r.text
