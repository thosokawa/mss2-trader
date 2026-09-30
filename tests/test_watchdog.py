"""株価の受信停止・凍結の検知と、約定のない tick から足を作らないこと（2026-09-30 の件）。"""
from datetime import datetime, timedelta

from sqlmodel import Session, select

from app.aggregator import build_bars
from app.db import engine, init_db
from app.engine import watchdog
from app.models import Bar, Strategy, Tick

# 2026-09-30(水) JST 09:00 = 00:00 UTC
OPEN = datetime(2026, 9, 30, 0, 0)


def _tick(s, code, ts, price, volume):
    s.add(Tick(symbol_code=code, ts=ts, price=price, volume=volume, received_at=ts))


def _strategy(s, code, mode="live"):
    s.add(Strategy(name=f"wd-{code}-{mode}", class_path="app.strategy.examples.sma_cross:SmaCross",
                   symbols=code, timeframe="1m", mode=mode, enabled=True, params_json="{}"))
    s.commit()


class _Mine:
    """テスト DB は他のテストと共有なので、この銘柄を含む通知だけ集める。"""

    def __init__(self, code, got):
        self.code, self.got = code, got

    def append(self, text):
        if self.code in text:
            self.got.append(text)
        return True


def test_frozen_quote_is_alerted_once_and_recovery_notified():
    init_db()
    watchdog._alerted.clear()
    code = "991A"
    got: list[str] = []
    sent = _Mine(code, got)
    with Session(engine) as s:
        _strategy(s, code)
        # 前日の大引け後から寄り付き後まで、前日の終値・累計出来高のまま（凍結）
        for i in range(0, 30):
            _tick(s, code, OPEN - timedelta(minutes=10) + timedelta(minutes=i), 6035.0, 64169100.0)
        s.commit()
        # 09:02 はまだ判定しない（寄り付きから3分経っていない）
        assert watchdog.check(s, now=OPEN + timedelta(minutes=2), send=sent.append).get(code) is None
        stale = watchdog.check(s, now=OPEN + timedelta(minutes=4), send=sent.append)
        assert code in stale and "更新されていません" in stale[code]
        assert len(got) == 1
        watchdog.check(s, now=OPEN + timedelta(minutes=5), send=sent.append)
        assert len(got) == 1  # 同じ状態のまま繰り返し送らない
        # 接続が戻る（当日の累計出来高に変わる）
        _tick(s, code, OPEN + timedelta(minutes=20, seconds=10), 6291.0, 12467700.0)
        s.commit()
        back = watchdog.check(s, now=OPEN + timedelta(minutes=20, seconds=20), send=sent.append)
        assert code not in back
        assert len(got) == 2 and "戻りました" in got[1]


def test_no_ticks_is_alerted_and_outside_session_is_ignored():
    init_db()
    watchdog._alerted.clear()
    code = "992A"
    got: list[str] = []
    sent = _Mine(code, got)
    with Session(engine) as s:
        _strategy(s, code, mode="paper")
        _tick(s, code, OPEN + timedelta(minutes=1), 100.0, 1000.0)
        s.commit()
        stale = watchdog.check(s, now=OPEN + timedelta(minutes=10), send=sent.append)
        assert "届いていません" in stale[code]
        # 昼休み・大引け前の板寄せ（15:25〜）・休日は判定しない
        quiet = (datetime(2026, 9, 30, 12, 0), datetime(2026, 9, 30, 15, 27), datetime(2026, 10, 3, 10, 0))
        for jst in quiet:
            assert watchdog.check(s, now=jst - timedelta(hours=9), send=sent.append) == {}


def test_notify_only_strategy_is_not_watched():
    init_db()
    watchdog._alerted.clear()
    with Session(engine) as s:
        _strategy(s, "993A", mode="notify")
        assert "993A" not in watchdog.watched_codes(s)


def test_bars_are_not_built_from_frozen_quotes():
    """夜通し・接続断の間の同じ値の tick（出来高が増えない）からは足を作らない。"""
    init_db()
    code = "994A"
    with Session(engine) as s:
        # 前日 15:29 に約定、15:30 に大引け（出来高が増える）
        close = OPEN - timedelta(hours=17, minutes=30)  # 前日 15:30 JST
        _tick(s, code, close - timedelta(minutes=1, seconds=-5), 5983.0, 58193600.0)
        _tick(s, code, close + timedelta(seconds=5), 6035.0, 64169100.0)
        # その後は翌朝 09:20 まで同じ値（凍結）
        t = close + timedelta(minutes=1)
        while t < OPEN + timedelta(minutes=20):
            _tick(s, code, t, 6035.0, 64169100.0)
            t += timedelta(minutes=1)
        # 09:20 に接続が戻る: 当日の累計出来高（前日より少ない＝日付でリセット）
        _tick(s, code, OPEN + timedelta(minutes=20, seconds=10), 6291.0, 12467700.0)
        _tick(s, code, OPEN + timedelta(minutes=20, seconds=40), 6280.0, 12500000.0)
        _tick(s, code, OPEN + timedelta(minutes=21, seconds=10), 6277.0, 12660100.0)
        s.commit()
        build_bars(s, timeframes=("1m",), lookback_minutes=24 * 60, now=OPEN + timedelta(minutes=22))
        q = select(Bar).where(Bar.symbol_code == code, Bar.timeframe == "1m").order_by(Bar.ts)
        bars = s.exec(q).all()
    jst = [(b.ts + timedelta(hours=9)).strftime("%m/%d %H:%M") for b in bars]
    assert jst == ["09/29 15:29", "09/29 15:30", "09/30 09:20", "09/30 09:21"]
    first_today = bars[2]
    assert first_today.open == 6291.0 and first_today.close == 6280.0
    assert first_today.volume == 12467700.0 + (12500000.0 - 12467700.0)
