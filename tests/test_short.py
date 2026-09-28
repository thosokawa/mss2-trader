"""空売り（売建 SHORT → 買戻し EXIT/COVER）の検証。"""
from datetime import datetime, timedelta
from unittest.mock import patch

import pandas as pd
import pytest
from sqlmodel import Session, select

from app.config import TradingCfg
from app.db import engine, init_db
from app.engine import live, orders, paper
from app.engine.backtest import run_backtest
from app.engine.risk import RiskEngine
from app.engine.stops import check_stop_target
from app.models import Bar, Order, PaperTrade, Strategy, Symbol, SymbolSet, SymbolSetItem
from app.strategy.base import Context, Position, Signal
from app.strategy.base import Strategy as BaseStrategy
from app.strategy.examples.sma_cross import SmaCross
from app.strategy.registry import BUILTIN, builtin_params, load_strategy_class


class AlwaysShort(BaseStrategy):
    """ノーポジなら毎足 SHORT、自分からは手仕舞わない。"""

    default_params = {"qty": 100, "allow_short": True}

    def on_bar(self, ctx: Context) -> Signal | None:
        return Signal("SHORT", reason="always short") if ctx.position.is_flat else None


def _bars(closes, start=datetime(2026, 3, 2, 0, 0), step=5):
    idx = pd.DatetimeIndex([start + timedelta(minutes=step * i) for i in range(len(closes))])
    return pd.DataFrame(
        {"open": closes, "high": closes, "low": closes, "close": closes, "volume": 1000.0}, index=idx
    )


# ---- Position / 損切り判定 ------------------------------------------------------


def test_position_direction():
    assert Position(100, 10).direction == 1 and Position(100, 10).is_long
    assert Position(-100, 10).direction == -1 and Position(-100, 10).is_short
    assert Position().direction == 0 and Position().is_flat


def test_short_stop_and_target_are_mirrored():
    # 売建 1000 円: 3% 上がったら損切り（高値で判定）
    hit = check_stop_target(1000.0, 1031.0, 1000.0, stop_loss_pct=3, direction=-1)
    assert hit is not None and hit.price == pytest.approx(1030.0) and "損切り" in hit.reason
    # 4% 下がったら利確（安値で判定）
    hit = check_stop_target(1000.0, 1000.0, 959.0, take_profit_pct=4, direction=-1)
    assert hit is not None and hit.price == pytest.approx(960.0) and "利確" in hit.reason
    # ロングなら損切りになる値動き（安値 959）は売建には無関係
    assert check_stop_target(1000.0, 1010.0, 959.0, stop_loss_pct=3, direction=-1) is None


# ---- 戦略 --------------------------------------------------------------------


def test_builtin_strategies_default_to_long_only_cash():
    for cp in BUILTIN.values():
        params = builtin_params()[cp]
        assert params["direction"] == "long"
        assert params["trade_type"] == "cash"
        strat = load_strategy_class(cp)(params)
        assert strat.allow_long and not strat.allow_short


def test_direction_and_legacy_allow_short():
    assert SmaCross({"direction": "short"}).direction == "short"
    assert SmaCross({"direction": "both"}).allow_long and SmaCross({"direction": "both"}).allow_short
    # 旧パラメータ allow_short だけが保存されている戦略
    assert SmaCross({"allow_short": True}).direction == "both"
    assert SmaCross({"allow_short": "false"}).direction == "long"
    assert SmaCross({}).direction == "long"
    # direction があればそちらが優先
    assert SmaCross({"direction": "long", "allow_short": True}).direction == "long"


class BuyThenShort(BaseStrategy):
    """ノーポジなら BUY と SHORT を交互に出す。方向の絞り込みの検証用。"""

    default_params = {"qty": 100}
    _n = 0

    def on_bar(self, ctx):
        if not ctx.position.is_flat:
            return Signal("EXIT", reason="exit")
        self._n += 1
        return Signal("BUY" if self._n % 2 else "SHORT", reason="entry")


def test_decide_filters_entries_by_direction():
    bars = _bars([100.0] * 8)
    for direction, want in (("long", {"LONG"}), ("short", {"SHORT"}), ("both", {"LONG", "SHORT"})):
        strat = BuyThenShort({"direction": direction})
        strat.timeframe = "5m"
        res = run_backtest(strat, bars, "X", warmup=0)
        assert {t.side for t in res.trades} == want, direction
    # 手仕舞い（EXIT）は方向に関係なく通す
    strat = BuyThenShort({"direction": "short"})
    sig = strat.decide(Context("X", bars.index[-1], bars, Position(100, 100.0), strat.params))
    assert sig is not None and sig.side == "EXIT"


def test_short_only_sma_cross_never_buys():
    up = [100 + i for i in range(25)]
    down = [124 - i * 3 for i in range(10)]
    up2 = [97 + i * 3 for i in range(10)]
    strat = SmaCross({"fast": 3, "slow": 10, "direction": "short"})
    strat.timeframe = "5m"
    res = run_backtest(strat, _bars(up + down + up2), "X", warmup=0)
    assert res.trades and all(t.side == "SHORT" for t in res.trades)


def test_sma_cross_shorts_on_dead_cross_only_when_allowed():
    up = [100 + i for i in range(25)]
    down = [124 - i * 3 for i in range(10)]
    bars = _bars(up + down)

    for allow, expect_short in ((False, False), (True, True)):
        strat = SmaCross({"fast": 3, "slow": 10, "allow_short": allow})
        sides = []
        for i in range(len(bars)):
            w = bars.iloc[: i + 1]
            sig = strat.on_bar(Context("X", w.index[-1], w, Position(), strat.params))
            if sig:
                sides.append(sig.side)
        assert ("SHORT" in sides) is expect_short


def test_sma_cross_covers_short_on_golden_cross():
    down = [200 - i for i in range(25)]
    up = [176 + i * 3 for i in range(10)]
    bars = _bars(down + up)
    strat = SmaCross({"fast": 3, "slow": 10, "allow_short": True})
    got = None
    for i in range(len(bars)):
        w = bars.iloc[: i + 1]
        got = got or strat.on_bar(Context("X", w.index[-1], w, Position(-100, 190.0), strat.params))
    assert got is not None and got.side == "EXIT"


# ---- バックテスト ------------------------------------------------------------------


def test_backtest_short_profit_when_price_falls():
    strat = AlwaysShort({"stop_loss_pct": None, "take_profit_pct": 5})
    strat.timeframe = "5m"
    res = run_backtest(strat, _bars([1000.0, 990.0, 970.0, 940.0, 930.0]), "X", warmup=0)
    t = res.trades[0]
    assert t.side == "SHORT"
    assert t.entry_price == 1000.0 and t.exit_price == pytest.approx(950.0)  # 5% 下で利確
    assert t.pnl == pytest.approx(50.0 * 100)
    assert t.return_pct == pytest.approx(5.0)


def test_backtest_short_stop_loss_when_price_rises():
    strat = AlwaysShort({"stop_loss_pct": 2})
    strat.timeframe = "5m"
    res = run_backtest(strat, _bars([1000.0, 1010.0, 1030.0]), "X", warmup=0)
    t = res.trades[0]
    assert t.exit_price == pytest.approx(1020.0)
    assert t.pnl == pytest.approx(-2000.0)
    assert "損切り" in t.reason_out


def test_backtest_short_flattened_at_close():
    from tests.test_eod import _session, utc

    times = _session(2026, 3, 2, (14, 0), (15, 25))
    strat = AlwaysShort({"hold_overnight": False})
    strat.timeframe = "5m"
    res = run_backtest(strat, _bars([1000.0] * len(times)).set_axis(pd.DatetimeIndex(times)), "X", warmup=0)
    assert res.trades[0].exit_ts == utc(2026, 3, 2, 15, 15)
    assert res.trades[0].reason_out == "大引け手仕舞い"


def test_backtest_equity_for_short_is_inverse():
    strat = AlwaysShort()
    strat.timeframe = "5m"
    res = run_backtest(strat, _bars([1000.0, 900.0]), "X", warmup=0)
    assert float(res.equity_curve.iloc[-1]) == pytest.approx(100.0 * 100)


# ---- ペーパー ------------------------------------------------------------------------


def _setup(s: Session, code: str, class_path: str, params: str, mode: str) -> Strategy:
    s.add(Symbol(code=code, name="テスト銘柄"))
    ss = SymbolSet(name=f"set-{code}")
    s.add(ss)
    s.commit()
    s.refresh(ss)
    s.add(SymbolSetItem(set_id=ss.id, symbol_code=code))
    st = Strategy(name=f"short-{code}", class_path=class_path, params_json=params,
                  symbol_set_id=ss.id, timeframe="5m", mode=mode, enabled=True)
    s.add(st)
    s.commit()
    s.refresh(st)
    return st


def _bar(s: Session, code: str, ts: datetime, c: float) -> datetime:
    s.add(Bar(symbol_code=code, timeframe="5m", ts=ts, open=c, high=c, low=c, close=c,
              volume=1000.0, source="rss"))
    s.commit()
    return ts + timedelta(minutes=5, seconds=30)


def test_paper_short_round_trip_with_stop():
    init_db()
    t0 = datetime(2026, 3, 2, 0, 0)
    with Session(engine) as s:
        st = _setup(s, "9821", "tests.test_short:AlwaysShort",
                    '{"qty": 100, "allow_short": true, "stop_loss_pct": 3}', "paper")
        live.run_once(s, notify=False, now=_bar(s, "9821", t0, 1000.0))  # カーソル初期化
        fired = live.run_once(s, notify=False, now=_bar(s, "9821", t0 + timedelta(minutes=5), 1000.0))
        assert [f.side for f in fired] == ["SHORT"]
        pos = paper.current_position(s, st.id, "9821")
        assert pos.is_short and pos.qty == -100

        fired = live.run_once(s, notify=False, now=_bar(s, "9821", t0 + timedelta(minutes=10), 1040.0))
        assert [f.side for f in fired] == ["COVER"]
        assert "損切り" in fired[0].reason
        t = s.exec(select(PaperTrade).where(PaperTrade.strategy_id == st.id)).one()
        assert t.side == "SHORT" and t.status == "closed"
        assert t.entry_price < 1000.0  # 売建は安く約定（スリッページ）
        assert t.exit_price > t.entry_price * 1.03  # 損切りライン（建値+3%）より高く買戻し
        assert t.pnl < 0


# ---- 実発注 --------------------------------------------------------------------------


def _armed():
    eng = RiskEngine(TradingCfg(enabled=True, max_qty_per_order=1000, max_notional_per_order=10_000_000,
                                daily_loss_limit=1_000_000, session_windows=["00:00-23:59"]))
    eng.arm()
    return eng


def test_live_short_blocked_for_cash_trade_type():
    init_db()
    t0 = datetime(2026, 3, 2, 0, 0)
    with Session(engine) as s, patch("app.engine.live.get_risk_engine", return_value=_armed()):
        st = _setup(s, "9822", "tests.test_short:AlwaysShort", '{"qty": 100, "allow_short": true}', "live")
        live.run_once(s, notify=False, now=_bar(s, "9822", t0, 1000.0))
        fired = live.run_once(s, notify=False, now=_bar(s, "9822", t0 + timedelta(minutes=5), 1000.0))
        assert "信用" in fired[0].reason and "発注見送り" in fired[0].reason
        assert s.exec(select(Order).where(Order.strategy_id == st.id)).all() == []


def test_live_margin_orders_blocked_when_disabled(monkeypatch):
    init_db()
    monkeypatch.setattr(live, "MARGIN_ORDERS_SUPPORTED", False)
    t0 = datetime(2026, 3, 2, 0, 0)
    with Session(engine) as s, patch("app.engine.live.get_risk_engine", return_value=_armed()):
        st = _setup(s, "9823", "tests.test_short:AlwaysShort",
                    '{"qty": 100, "allow_short": true, "trade_type": "margin"}', "live")
        live.run_once(s, notify=False, now=_bar(s, "9823", t0, 1000.0))
        fired = live.run_once(s, notify=False, now=_bar(s, "9823", t0 + timedelta(minutes=5), 1000.0))
        assert "信用の実発注はまだ未対応" in fired[0].reason
        assert s.exec(select(Order).where(Order.strategy_id == st.id)).all() == []


def test_live_margin_short_queues_order_with_margin_type():
    """信用区分を付けて売建し、損切りで買戻す（買戻しには建てた日を付ける）。"""
    init_db()
    assert live.MARGIN_ORDERS_SUPPORTED
    eng = _armed()
    t0 = datetime(2026, 3, 2, 0, 0)
    with Session(engine) as s, patch("app.engine.live.get_risk_engine", return_value=eng), \
            patch("app.engine.orders.get_risk_engine", return_value=eng):
        st = _setup(s, "9824", "tests.test_short:AlwaysShort",
                    '{"qty": 100, "allow_short": true, "trade_type": "margin", "stop_loss_pct": 3,'
                    ' "hold_overnight": false}', "live")
        live.run_once(s, notify=False, now=_bar(s, "9824", t0, 1000.0))
        live.run_once(s, notify=False, now=_bar(s, "9824", t0 + timedelta(minutes=5), 1000.0))
        o = s.exec(select(Order).where(Order.strategy_id == st.id)).one()
        assert (o.side, o.trade_type, o.margin_type) == ("SHORT", "margin", 4)  # いちにち信用

        orders.claim_pending(s)
        orders.apply_report(s, o.id, status="sent")
        assert orders.current_live_position(s, st.id, "9824").is_short

        fired = live.run_once(s, notify=False, now=_bar(s, "9824", t0 + timedelta(minutes=10), 1040.0))
        assert [f.side for f in fired] == ["COVER"]
        cover = s.exec(select(Order).where(Order.strategy_id == st.id, Order.side == "COVER")).one()
        assert (cover.trade_type, cover.margin_type) == ("margin", 4)  # 建てたときに合わせる
        assert cover.open_date == orders.jst_yyyymmdd(o.ts)
        orders.claim_pending(s)
        orders.apply_report(s, cover.id, status="sent")
        assert eng.state.day_realized_pnl == pytest.approx(-3000.0)  # (1030-1000)×100 の損
        assert orders.current_live_position(s, st.id, "9824").is_flat


def test_margin_type_for_overnight():
    assert live.margin_type_for({"hold_overnight": False}, "5m") == 4
    assert live.margin_type_for({"hold_overnight": True}, "5m") == 2
    assert live.margin_type_for({"hold_overnight": False}, "1d") == 2  # 日足は持ち越す


def test_bridge_rejects_impossible_orders_without_touching_excel():
    from tests.test_bridge_orders import bridge

    relay = bridge.OrderRelay("does-not-exist.xlsx")
    for order in (
        {"id": 1, "symbol_code": "9984", "side": "SHORT", "qty": 100, "trade_type": "cash"},
        {"id": 2, "symbol_code": "9984", "side": "COVER", "qty": 100, "trade_type": "cash"},
        {"id": 3, "symbol_code": "9984", "side": "BUY", "qty": 100, "trade_type": "fx"},
    ):
        res = relay.place(order, 1.0)  # Excel を開こうとすれば例外になる
        assert res["status"] == "rejected" and "未対応" in res["error"]
