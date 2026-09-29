"""手動決済の記録（/risk）・建玉がある戦略の削除ガード・銘柄セットからのデータ移行。"""
import sqlite3

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import engine, init_db
from app.engine import orders
from app.main import app
from app.models import Order, Strategy


def _live_strategy(s: Session, name: str, code: str) -> Strategy:
    st = Strategy(name=name, class_path="app.strategy.examples.sma_cross:SmaCross",
                  symbols=code, timeframe="1m", mode="live", enabled=True)
    s.add(st)
    s.commit()
    s.refresh(st)
    return st


def _accepted_buy(s: Session, st: Strategy, code: str, trade_type: str = "margin") -> Order:
    o = orders.queue_order(s, st, code, "BUY", 100, "MACD上抜け", ref_price=6066.0,
                           trade_type=trade_type, margin_type=4 if trade_type == "margin" else 0)
    o.status = "sent"  # bridge が受理を報告した状態
    s.add(o)
    s.commit()
    return o


def test_open_positions_and_manual_close():
    init_db()
    with Session(engine) as s:
        st = _live_strategy(s, "手動決済テスト", "9901")
        _accepted_buy(s, st, "9901")
        mine = [p for p in orders.open_positions(s) if p["strategy_id"] == st.id]
        assert len(mine) == 1 and mine[0]["position"].qty == 100 and mine[0]["strategy_exists"]

        rec = orders.record_manual_close(s, st.id, "9901")
        assert rec.status == orders.MANUAL and rec.side == "EXIT" and rec.qty == 100
        assert rec.trade_type == "margin"
        assert orders.current_live_position(s, st.id, "9901").is_flat
        assert not orders.has_in_flight_order(s, st.id, "9901")
        # 記録は発注されない（bridge は new しか拾わない）
        assert rec.id not in [o.id for o in orders.claim_pending(s)]
        # 2回目は何もしない
        assert orders.record_manual_close(s, st.id, "9901") is None


def test_manual_close_via_risk_page_and_deleted_strategy():
    init_db()
    with Session(engine) as s:
        st = _live_strategy(s, "削除済み戦略テスト", "9902")
        _accepted_buy(s, st, "9902")
        sid = st.id
    with TestClient(app) as c:
        # 建玉がある戦略は削除できない
        r = c.post(f"/strategies/{sid}/delete", follow_redirects=True)
        assert "建玉を持っています" in r.text
        # 建玉のある銘柄を外すこともできない
        r = c.post(f"/strategies/{sid}/symbols", data={"symbols": "9999"}, follow_redirects=True)
        assert "建玉を持っています" in r.text

        r = c.get("/risk")
        assert "bot が認識している建玉" in r.text and "削除済み戦略テスト" in r.text
        r = c.post("/risk/manual-close", data={"strategy_id": sid, "symbol_code": "9902"},
                   follow_redirects=True)
        assert r.status_code == 200
        # 決済済みにすれば削除できる
        r = c.post(f"/strategies/{sid}/delete", follow_redirects=True)
        assert "削除済み戦略テスト" not in r.text.split("bot が認識している建玉")[0]
    with Session(engine) as s:
        assert orders.current_live_position(s, sid, "9902").is_flat
        # 戦略を消しても注文の記録は残る
        assert s.exec(select(Order).where(Order.strategy_id == sid)).all()


def test_migrate_symbol_sets_to_strategy_symbols_and_watch(tmp_path, monkeypatch):
    """旧 DB（銘柄セットあり・新しい列なし）を移行すると、戦略の銘柄と監視銘柄に写る。"""
    from sqlmodel import create_engine

    import app.db as db

    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE symbol (code VARCHAR PRIMARY KEY, name VARCHAR, market VARCHAR, tick_size FLOAT);
        CREATE TABLE symbolset (id INTEGER PRIMARY KEY, name VARCHAR, note VARCHAR, created_at DATETIME);
        CREATE TABLE symbolsetitem (id INTEGER PRIMARY KEY, set_id INTEGER, symbol_code VARCHAR,
                                    sort_order INTEGER);
        CREATE TABLE strategy (id INTEGER PRIMARY KEY, name VARCHAR, class_path VARCHAR, params_json VARCHAR,
                               symbol_set_id INTEGER, timeframe VARCHAR, mode VARCHAR, enabled BOOLEAN,
                               created_at DATETIME);
        INSERT INTO symbol VALUES ('9984','ソフトバンクグループ','東証',1.0), ('5016','JX金属','東証',1.0);
        INSERT INTO symbolset VALUES (1,'RSS監視','',NULL), (2,'自動売買','',NULL);
        INSERT INTO symbolsetitem VALUES (1,1,'9984',0), (2,2,'9984',0), (3,2,'5016',1);
        INSERT INTO strategy VALUES (1,'SBG','x:Y','{}',2,'1m','live',1,NULL);
    """)
    con.commit()
    con.close()

    monkeypatch.setattr(db, "engine", create_engine(f"sqlite:///{path}"))
    db.init_db()
    con = sqlite3.connect(path)
    assert con.execute("SELECT symbols FROM strategy WHERE id=1").fetchone()[0] == "9984,5016"
    # どの戦略も使っていない銘柄セット（旧 RSS監視）の銘柄が監視銘柄になる
    assert dict(con.execute("SELECT code, watch FROM symbol").fetchall()) == {"9984": 1, "5016": 0}
    con.close()
    db.init_db()  # 2回目は何もしない（列は追加済み）


def test_quote_codes_is_union_of_enabled_strategies_and_watch():
    from app.models import Symbol
    from app.symbols import quote_codes

    init_db()
    with Session(engine) as s:
        s.add(Strategy(name="有効", class_path="a:B", symbols="9911,9912", enabled=True))
        s.add(Strategy(name="無効", class_path="a:B", symbols="9913", enabled=False))
        s.add(Symbol(code="9914", name="", watch=True))
        s.commit()
        codes = quote_codes(s)
    assert {"9911", "9912", "9914"} <= set(codes) and "9913" not in codes
    assert codes == sorted(codes)
