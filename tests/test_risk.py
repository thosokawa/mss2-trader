from datetime import date, datetime, timedelta

from app.config import TradingCfg
from app.engine.risk import RiskEngine, get_risk_engine


def _eng(**over):
    cfg = TradingCfg(enabled=True, max_qty_per_order=100, max_notional_per_order=300_000,
                     daily_loss_limit=30_000, **over)
    return RiskEngine(cfg)


def jst(*a) -> datetime:
    """check() の now は naive UTC。テストは JST で書いて変換する。"""
    return datetime(*a) - timedelta(hours=9)


def test_disarmed_by_default():
    e = _eng()
    ok, why = e.check(now=jst(2026, 1, 5, 10, 0), side="BUY", qty=100, price=2000, mode="live")
    assert not ok and why == "DISARMED"


def test_armed_within_limits_passes():
    e = _eng()
    e.arm()
    ok, why = e.check(now=jst(2026, 1, 5, 10, 0), side="BUY", qty=100, price=2000, mode="live")
    assert ok, why


def test_notify_mode_never_orders():
    e = _eng()
    e.arm()
    ok, _ = e.check(now=jst(2026, 1, 5, 10, 0), side="BUY", qty=100, price=2000, mode="notify")
    assert not ok


def test_outside_session_blocked():
    e = _eng()
    e.arm()
    ok, why = e.check(now=jst(2026, 1, 5, 18, 0), side="BUY", qty=100, price=2000, mode="live")
    assert not ok and "時間外" in why


def test_notional_cap():
    e = _eng()
    e.arm()
    ok, why = e.check(now=jst(2026, 1, 5, 10, 0), side="BUY", qty=100, price=5000, mode="live")
    assert not ok and "金額" in why


def test_daily_loss_limit_disarms():
    e = _eng()  # daily_loss_limit=30_000（既定）
    e.arm()
    e.record_fill_pnl(-32_000, today=date(2026, 1, 5))
    assert not e.state.armed
    ok, why = e.check(now=jst(2026, 1, 5, 10, 0), side="BUY", qty=1, price=100, mode="live")
    assert not ok


def test_daily_loss_resets_on_new_day():
    e = _eng()
    e.arm()
    e.record_fill_pnl(-32_000, today=date(2026, 1, 5))
    assert not e.state.armed
    e.arm()  # 翌日、手動で再アーム
    ok, why = e.check(now=jst(2026, 1, 6, 10, 0), side="BUY", qty=100, price=2000, mode="live")
    assert ok, why
    assert e.state.day_realized_pnl == 0.0


def test_get_risk_engine_is_singleton():
    a = get_risk_engine()
    b = get_risk_engine()
    assert a is b


def test_session_is_judged_in_jst():
    # 09:51 JST = 00:51 UTC（実機で「取引時間外」と誤判定されたケース）
    e = _eng()
    e.arm()
    ok, why = e.check(now=datetime(2026, 9, 28, 0, 51), side="BUY", qty=100, price=2000, mode="live")
    assert ok, why
    # 18:00 UTC = 翌 03:00 JST は時間外
    ok, why = e.check(now=datetime(2026, 9, 28, 18, 0), side="BUY", qty=100, price=2000, mode="live")
    assert not ok and "時間外" in why
    # 昼休み 11:45 JST = 02:45 UTC も時間外
    ok, why = e.check(now=datetime(2026, 9, 28, 2, 45), side="BUY", qty=100, price=2000, mode="live")
    assert not ok and "時間外" in why
