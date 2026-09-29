"""画面上部のステータスバーとダッシュボードが使う「システム全体の今の状態」の集計。

自動売買が実際に動いているか（config.trading.enabled × ARMED × 有効な live 戦略）、
取引時間か、bridge から株価が届いているか、bot の建玉・本日の発注・損益をまとめる。
"""
from __future__ import annotations

from datetime import datetime, time, timedelta

from sqlmodel import Session, func, select

from app.engine import orders as orders_engine
from app.engine.live import MARGIN_ORDERS_SUPPORTED
from app.engine.risk import JST_OFFSET, get_risk_engine
from app.models import Order, Signal, Strategy, Tick, utcnow

BRIDGE_STALE_SEC = 30


def jst_day_start_utc(now_utc: datetime) -> datetime:
    """JST の今日 0:00 を naive UTC で。「本日の〜」の集計に使う。"""
    jst = now_utc + JST_OFFSET
    return datetime.combine(jst.date(), time(0, 0)) - JST_OFFSET


def market_phase(now_utc: datetime, session_windows: list[str]) -> tuple[str, bool]:
    """(表示名, 取引時間内か)。取引時間は config.trading.session_windows（JST）。"""
    t = (now_utc + JST_OFFSET).time()
    weekday = (now_utc + JST_OFFSET).weekday()
    if weekday >= 5:
        return "休日", False
    windows = []
    for w in session_windows:
        a, b = w.split("-")
        windows.append((time.fromisoformat(a), time.fromisoformat(b)))
    for a, b in windows:
        if a <= t <= b:
            return "取引時間中", True
    if windows and windows[0][1] < t < windows[-1][0]:
        return "昼休み", False
    if windows and t < windows[0][0]:
        return "寄り前", False
    return "取引時間外", False


def system_status(s: Session, now_utc: datetime | None = None) -> dict:
    now = now_utc or utcnow()
    eng = get_risk_engine()
    cfg = eng.cfg
    day0 = jst_day_start_utc(now)

    enabled = s.exec(select(Strategy).where(Strategy.enabled == True)).all()  # noqa: E712
    live_enabled = [st for st in enabled if st.mode == "live"]
    phase, in_session = market_phase(now, cfg.session_windows)

    last_tick = s.exec(select(func.max(Tick.received_at))).one()
    tick_age = (now - last_tick).total_seconds() if last_tick else None

    today_orders = s.exec(select(Order).where(Order.ts >= day0)).all()
    positions = orders_engine.open_positions(s)
    today_signals = s.exec(
        select(func.count()).select_from(Signal).where(Signal.ts >= day0)
    ).one()

    # 自動売買の総合状態（ステータスバーの色と文言）
    if not cfg.enabled:
        level, label = "off", "自動売買 無効（config.trading.enabled=false）"
    elif eng.state.armed and live_enabled:
        level, label = "on", f"自動売買 稼働中（live 戦略 {len(live_enabled)}）"
    elif eng.state.armed:
        level, label = "warn", "ARM 中（有効な live 戦略なし）"
    elif live_enabled:
        level, label = "warn", f"DISARM 中 — live 戦略 {len(live_enabled)} は発注しない"
    else:
        level, label = "off", "自動売買 停止中"

    if tick_age is None:
        bridge = {"level": "off", "label": "株価 未受信"}
    elif tick_age > BRIDGE_STALE_SEC:
        bridge = {"level": "warn" if not in_session else "bad",
                  "label": f"株価 {_age_text(tick_age)}前"}
    else:
        bridge = {"level": "on", "label": f"株価 受信中（{tick_age:.0f}秒前）"}

    return {
        "level": level,
        "label": label,
        "halted_reason": eng.state.halted_reason if not eng.state.armed else "",
        "config_enabled": cfg.enabled,
        "armed": eng.state.armed,
        "live_enabled": live_enabled,
        "enabled_n": len(enabled),
        "phase": phase,
        "in_session": in_session,
        "bridge": bridge,
        "tick_age": tick_age,
        "last_tick": last_tick,
        "positions": positions,
        "today_orders": today_orders,
        "today_sent": sum(1 for o in today_orders if o.status in ("sent", "filled")),
        "today_failed": sum(1 for o in today_orders if o.status in ("rejected", "error", "timeout")),
        "today_pending": sum(1 for o in today_orders if o.status in ("new", "sending")),
        "today_signals": today_signals,
        "day_pnl": eng.state.day_realized_pnl,
        "margin_supported": MARGIN_ORDERS_SUPPORTED,
        "now": now,
    }


def strategy_overview(s: Session) -> list[dict]:
    """有効な戦略ごとの状況（対象銘柄の現在値・建玉・最後のシグナル）。ダッシュボード用。"""
    import json

    from app.engine import paper
    from app.engine.live import position_from_signals
    from app.models import Symbol
    from app.symbols import parse_codes

    names = {sym.code: sym.name for sym in s.exec(select(Symbol)).all()}
    out = []
    enabled = select(Strategy).where(Strategy.enabled == True, Strategy.deleted == False)  # noqa: E712
    for st in s.exec(enabled.order_by(Strategy.name)).all():
        try:
            params = json.loads(st.params_json or "{}")
        except ValueError:
            params = {}
        qty_hint = int(params.get("qty", 100) or 100)
        rows = []
        for code in parse_codes(st.symbols):
            if st.mode == "live":
                pos = orders_engine.current_live_position(s, st.id, code, qty_hint)
            elif st.mode == "paper":
                pos = paper.current_position(s, st.id, code)
            else:
                pos = position_from_signals(s, st.id, code, qty_hint)
            tick = s.exec(
                select(Tick).where(Tick.symbol_code == code, Tick.price > 0).order_by(Tick.ts.desc())
            ).first()
            last = tick.price if tick else None
            unrealized = (
                (last - pos.avg_price) * abs(pos.qty) * pos.direction
                if last and not pos.is_flat and pos.avg_price else None
            )
            rows.append({"code": code, "name": names.get(code, ""), "price": last,
                         "tick_ts": tick.received_at if tick else None, "pos": pos,
                         "unrealized": unrealized})
        codes = [r["code"] for r in rows]
        last_sig = s.exec(
            select(Signal)
            .where(Signal.strategy_id == st.id, Signal.symbol_code.in_(codes))
            .order_by(Signal.ts.desc(), Signal.id.desc())
        ).first() if codes else None
        out.append({
            "st": st, "rows": rows, "last_signal": last_sig,
            "direction": params.get("direction") or ("both" if params.get("allow_short") else "long"),
            "trade_type": params.get("trade_type") or "cash",
            "hold_overnight": params.get("hold_overnight", True),
        })
    return out


def _age_text(sec: float) -> str:
    if sec < 90:
        return f"{sec:.0f}秒"
    if sec < 3600 * 2:
        return f"{sec / 60:.0f}分"
    if sec < 86400 * 2:
        return f"{sec / 3600:.0f}時間"
    return f"{sec / 86400:.0f}日"


def since_text(dt: datetime | None, now_utc: datetime | None = None) -> str:
    if dt is None:
        return "—"
    return _age_text(((now_utc or utcnow()) - dt) / timedelta(seconds=1)) + "前"
