"""株価の受信が止まった・凍結したことを検知して通知する（Slack / メール）。

RSS は Excel と MarketSpeed II の接続が切れても最後の値を返し続けるので、bridge は同じ値を
送り続け「受信中」に見える（2026-09-30: 寄り付きから 09:23 まで前日終値のまま凍結していた）。
そこで取引時間中に次のどちらかが STALE_SEC 続いたら「止まっている」とみなす:

- tick が届いていない（bridge / Excel が止まっている）
- 累計出来高が変わらない（RSS の値が凍結している。約定があれば出来高は増える）

対象は「有効な 実発注・ペーパー の戦略の対象銘柄」と「bot の建玉がある銘柄」。
止まった銘柄をまとめて1回通知し、戻ったらもう1回通知する（同じ状態のまま繰り返し送らない）。
大引け前の板寄せ（15:25〜15:30）は約定が無いので判定しない。
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, time, timedelta

from sqlmodel import Session, select

from app import notify
from app.engine import orders as orders_engine
from app.engine.risk import get_risk_engine
from app.models import Strategy, Tick, utcnow
from app.symbols import parse_codes

log = logging.getLogger("watchdog")

JST_OFFSET = timedelta(hours=9)
STALE_SEC = 180
CLOSING_AUCTION = timedelta(minutes=5)  # 最後の取引時間帯の終わりの板寄せ（約定なし）

_alerted: set[str] = set()  # いま「止まっている」と通知済みの銘柄


def _session_start(now_utc: datetime, windows: list[str]) -> datetime | None:
    """取引時間中なら、いまの時間帯の開始（naive UTC）。時間外・休日・大引け前の板寄せ中は None。"""
    jst = now_utc + JST_OFFSET
    if jst.weekday() >= 5:
        return None
    parsed = []
    for w in windows:
        a, b = w.split("-")
        parsed.append((time.fromisoformat(a), time.fromisoformat(b)))
    for i, (a, b) in enumerate(parsed):
        start = datetime.combine(jst.date(), a)
        end = datetime.combine(jst.date(), b)
        if i == len(parsed) - 1:
            end -= CLOSING_AUCTION
        if start <= jst < end:
            return start - JST_OFFSET
    return None


def watched_codes(s: Session) -> list[str]:
    codes: list[str] = []
    for st in s.exec(select(Strategy).where(Strategy.enabled == True)).all():  # noqa: E712
        if st.mode in ("live", "paper") and not st.deleted:
            codes += parse_codes(st.symbols)
    codes += [p["symbol_code"] for p in orders_engine.open_positions(s)]
    return sorted(set(codes))


def stale_reason(s: Session, code: str, now: datetime, since: datetime) -> str | None:
    """止まっていれば理由の文、動いていれば None。since = いまの取引時間帯の開始。"""
    last = s.exec(
        select(Tick).where(Tick.symbol_code == code, Tick.ts <= now).order_by(Tick.ts.desc()).limit(1)
    ).first()
    if last is None or (now - last.received_at).total_seconds() >= STALE_SEC:
        if (now - since).total_seconds() < STALE_SEC:
            return None
        ago = "一度も" if last is None else f"{int((now - last.received_at).total_seconds() // 60)}分"
        return f"株価が届いていません（{ago}受信なし）"
    if not last.volume:
        return None  # 出来高の情報が無い（シミュレーション等）は凍結を判定しない
    changed = s.exec(
        select(Tick.ts).where(Tick.symbol_code == code, Tick.ts >= since, Tick.ts <= now,
                              Tick.volume != last.volume)
        .order_by(Tick.ts.desc()).limit(1)
    ).first()
    moved_at = changed or since
    frozen = (now - moved_at).total_seconds()
    if frozen >= STALE_SEC:
        return (f"株価が更新されていません（{int(frozen // 60)}分間 出来高が変わらず、"
                f"株価 {last.price:,.0f} のまま。Excel と MarketSpeed II の接続切れの可能性）")
    return None


def check(
    s: Session, now: datetime | None = None, send: Callable[[str], bool] | None = None
) -> dict[str, str]:
    """止まっている銘柄 {code: 理由} を返す。状態が変わったときだけ通知する。"""
    now = now or utcnow()
    send = send or notify.send
    since = _session_start(now, get_risk_engine().cfg.session_windows)
    if since is None:
        _alerted.clear()  # 取引時間外は判定しない（次の取引時間帯で改めて）
        return {}
    stale: dict[str, str] = {}
    for code in watched_codes(s):
        reason = stale_reason(s, code, now, since)
        if reason:
            stale[code] = reason
    new = [c for c in stale if c not in _alerted]
    recovered = [c for c in _alerted if c not in stale]
    if new:
        lines = [f"・{c}: {stale[c]}" for c in new]
        text = ("*⚠ 株価の受信が止まっています*\n" + "\n".join(lines) +
                "\nこの間はシグナル・損切り・大引け手仕舞いが出ません。"
                "建玉があれば MarketSpeed II で確認してください。")
        log.warning(text)
        send(text)
    if recovered:
        text = "*株価の受信が戻りました*: " + "、".join(recovered)
        log.info(text)
        send(text)
    _alerted.clear()
    _alerted.update(stale)
    return stale
