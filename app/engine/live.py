"""ライブのシグナル生成ループ（P2、P4で実発注を追加）。

流れ:
  bridge から届いた tick は aggregator が確定足（Bar）にしている。
  live エンジンはそれを読んで、enabled な Strategy ごとに:
    対象銘柄（Strategy.symbols）の各銘柄の「まだ評価していない確定足」を on_bar に流す
    -> Signal が返ったら:
       - Signal を DB 保存（origin="live", idempotency_key で重複防止）
       - notify.send で通知（Slack / メール）
       - mode=paper: PaperBroker で擬似約定
       - mode=live : RiskEngine.check() を通過し、決着待ちの発注が無ければ
         Order をキューイング（実際の発注は bridge が非同期で行う。P4）
  ポジションは mode ごとに別の情報源から復元する
  （notify: live シグナル履歴 / paper: PaperTrade / live: Order 履歴）。

バックテストと同じ Strategy.on_bar を呼ぶので挙動が一致する。
損切り/利確（stops.py）と大引け手仕舞い（eod.py、hold_overnight=False の日中足）も
バックテストと同じ判定を on_bar より先に行う。live では加えて、前日から持ち越して
しまった建玉（大引け時に backend が止まっていた等）を翌日最初の足で手仕舞いする。
mode=live では、足の確定から trading.max_signal_age_sec 以上たったシグナルは発注しない
（backend 停止後にまとめて評価された古い足のシグナルを、今の成行で発注しないため）。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

import pandas as pd
from sqlmodel import Session, select

from app.aggregator import TF_DELTA
from app.bars import load_bars
from app.config import get_config
from app.engine import orders, paper
from app.engine.eod import flatten_at_close, is_last_bar_of_day, is_new_day
from app.engine.risk import get_risk_engine
from app.engine.stops import check_stop_target
from app.models import LiveCursor, Signal, Strategy, Symbol, utcnow
from app.notify import format_signal, send
from app.strategy.base import Context, Position
from app.strategy.registry import load_strategy_class

log = logging.getLogger("live")


def evaluate_latest(strategy, bars: pd.DataFrame, symbol: str, position: Position | None = None):
    """確定済み足の列に対して最新1本ぶんの判定を返す。単発評価ユーティリティ。"""
    if bars.empty:
        return None
    now = pd.Timestamp(bars.index[-1]).to_pydatetime()
    ctx = Context(symbol=symbol, now=now, bars=bars, position=position or Position(), params=strategy.params)
    return strategy.decide(ctx)


def idempotency_key(strategy_name: str, symbol: str, bar_ts: datetime) -> str:
    return f"{strategy_name}:{symbol}:{bar_ts.isoformat()}"


def position_from_signals(
    session: Session, strategy_id: int, symbol_code: str, qty_hint: int = 100
) -> Position:
    """live シグナル履歴を畳んで現在ポジションを求める（売建は qty マイナス）。"""
    rows = session.exec(
        select(Signal)
        .where(
            Signal.strategy_id == strategy_id,
            Signal.symbol_code == symbol_code,
            Signal.origin == "live",
        )
        .order_by(Signal.ts, Signal.id)
    ).all()
    pos = Position()
    for s in rows:
        if s.side == "BUY" and pos.is_flat:
            pos = Position(qty=qty_hint, avg_price=s.price)
        elif s.side == "SHORT" and pos.is_flat:
            pos = Position(qty=-qty_hint, avg_price=s.price)
        elif s.side in ("EXIT", "SELL") and pos.is_long:
            pos = Position()
        elif s.side in ("EXIT", "COVER") and pos.is_short:
            pos = Position()
    return pos


OPEN_SIDES = {"BUY": 1, "SHORT": -1}
CLOSE_SIDES = {"EXIT", "SELL", "COVER"}

# 信用の新規建てを発注してよいか。False にすると信用の新規建てだけ「発注見送り」になる
# （手仕舞い・現物には影響しない）。
# 2026-09-29 実機で判明: RSS の一覧関数（RssMarginPositionList 等）は MarketSpeed II の
# 「更新」アイコンを押したときしか最新にならず、bridge が返済に必要な建日・建単価を取れない
# （bot の買建が一覧に載らず返済が「該当0株」で失敗した）。自動返済の手段が見つかるまで False。
MARGIN_ORDERS_SUPPORTED = False


def margin_type_for(params: dict, timeframe: str) -> int:
    """信用区分（RssMarginOpenOrder）: 大引けをまたがないならいちにち信用(4)、またぐなら
    一般信用・無期限(2)。"""
    return 4 if flatten_at_close(params, timeframe) else 2


def _symbols_for(session: Session, strategy: Strategy) -> list[str]:
    from app.symbols import parse_codes

    return parse_codes(strategy.symbols)


def _cursor(session: Session, strategy_id: int, symbol_code: str) -> LiveCursor | None:
    return session.exec(
        select(LiveCursor).where(
            LiveCursor.strategy_id == strategy_id, LiveCursor.symbol_code == symbol_code
        )
    ).first()


def _run_strategy_symbol(
    session: Session,
    strat_row: Strategy,
    strat,
    symbol_code: str,
    *,
    notify: bool,
    now: datetime | None = None,
) -> list[Signal]:
    tf = strat_row.timeframe
    bars = load_bars(session, symbol_code, tf)
    if bars.empty:
        return []

    latest_ts = pd.Timestamp(bars.index[-1]).to_pydatetime()
    cur = _cursor(session, strat_row.id, symbol_code)
    if cur is None:
        # 初回：発火させず、最新確定足まで見たことにするだけ。
        session.add(LiveCursor(strategy_id=strat_row.id, symbol_code=symbol_code, last_bar_ts=latest_ts))
        session.commit()
        return []
    if latest_ts <= cur.last_bar_ts:
        return []

    qty_hint = int(strat.params.get("qty", 100))
    is_paper = strat_row.mode == "paper"
    is_live_trading = strat_row.mode == "live"
    if is_paper:
        pos = paper.current_position(session, strat_row.id, symbol_code)
    elif is_live_trading:
        pos = orders.current_live_position(session, strat_row.id, symbol_code, qty_hint)
    else:
        pos = position_from_signals(session, strat_row.id, symbol_code, qty_hint)
    name = getattr(session.get(Symbol, symbol_code), "name", "") or ""

    all_ts = [pd.Timestamp(t).to_pydatetime() for t in bars.index]
    new_ts = [t for t in all_ts if t > cur.last_bar_ts]
    flatten_eod = flatten_at_close(strat.params, tf)
    fired: list[Signal] = []
    trade_type = str(strat.params.get("trade_type") or "cash")
    for ts in new_ts:
        window = bars.loc[:ts]
        bar_high = float(window["high"].iloc[-1])
        bar_low = float(window["low"].iloc[-1])
        close_px = float(window["close"].iloc[-1])
        eod = flatten_eod and is_last_bar_of_day(ts, tf)
        prev_ts = pd.Timestamp(window.index[-2]).to_pydatetime() if len(window) >= 2 else None
        carried = flatten_eod and not pos.is_flat and is_new_day(prev_ts, ts)

        # 損切り/利確（stop_loss_pct / take_profit_pct）は on_bar の判断より優先する。
        hit = (
            check_stop_target(
                pos.avg_price, bar_high, bar_low,
                stop_loss_pct=strat.params.get("stop_loss_pct"),
                take_profit_pct=strat.params.get("take_profit_pct"),
                direction=pos.direction,
            )
            if not pos.is_flat
            else None
        )
        if hit is not None:
            side, price, reason = "EXIT", hit.price, hit.reason
        elif not pos.is_flat and (eod or carried):
            reason = "大引け手仕舞い" if eod else "持ち越し建玉の手仕舞い"
            side, price = "EXIT", close_px
        else:
            ctx = Context(symbol=symbol_code, now=ts, bars=window, position=pos, params=strat.params)
            sig = strat.decide(ctx)
            if sig is None or (eod and sig.side in OPEN_SIDES):
                continue
            if sig.side == "SELL" and pos.is_short:
                continue  # 旧来の SELL は買い建玉の手仕舞いとしてだけ扱う
            side, price, reason = sig.side, close_px, sig.reason
        if side in ("EXIT", "COVER") and pos.is_short:
            side = "COVER"  # 売建の手仕舞い＝買戻し

        key = idempotency_key(strat_row.name, symbol_code, ts)
        if session.exec(select(Signal).where(Signal.idempotency_key == key)).first():
            continue

        blocked_reason = ""
        if is_live_trading:
            age = (now or utcnow()) - (ts + TF_DELTA.get(tf, timedelta(0)))
            max_age = get_config().trading.max_signal_age_sec
            if age.total_seconds() > max_age:
                blocked_reason = f"古い足のシグナル（確定から{int(age.total_seconds())}秒 > {max_age}秒）"
            elif side in OPEN_SIDES and not pos.is_flat:
                blocked_reason = "建玉あり（追加で建てない）"
            elif side in CLOSE_SIDES and pos.is_flat:
                blocked_reason = "建玉なし（手仕舞うものがない）"
            elif side == "SHORT" and trade_type != "margin":
                blocked_reason = "空売りは取引区分=margin（信用）のときだけ"
            elif side in OPEN_SIDES and trade_type == "margin" and not MARGIN_ORDERS_SUPPORTED:
                blocked_reason = "信用の実発注はまだ未対応（bridge の信用返済が実装待ち）"
            elif orders.has_in_flight_order(session, strat_row.id, symbol_code):
                blocked_reason = "決着待ちの発注が残っています"
            else:
                ok, why = get_risk_engine().check(now=ts, side=side, qty=qty_hint, price=price, mode="live")
                if not ok:
                    blocked_reason = why

        signal_reason = f"{reason} [発注見送り: {blocked_reason}]" if blocked_reason else reason
        row = Signal(
            strategy_id=strat_row.id,
            strategy_name=strat_row.name,
            symbol_code=symbol_code,
            ts=ts,
            side=side,
            reason=signal_reason,
            price=price,
            origin="live",
            idempotency_key=key,
        )
        session.add(row)
        session.commit()
        fired.append(row)
        pnl = None
        if is_paper:
            trade = paper.on_signal(session, strat_row, symbol_code, side, price, reason, ts, qty_hint)
            if trade is not None and trade.status == "closed" and side in CLOSE_SIDES:
                pnl = {  # ペーパー約定の実際の損益（スリッページ込み）
                    "pnl": trade.pnl or 0.0, "return_pct": trade.return_pct or 0.0,
                    "entry": trade.entry_price, "exit": trade.exit_price or price,
                    "qty": trade.qty, "short": trade.side == "SHORT",
                }
            pos = paper.current_position(session, strat_row.id, symbol_code)
        elif is_live_trading:
            if not blocked_reason:
                open_date = 0
                if side in OPEN_SIDES:
                    o_type, m_type = trade_type, margin_type_for(strat.params, tf)
                else:
                    # 手仕舞いは建てたときの取引区分・信用区分に合わせ、建てた日を渡す
                    # （bridge が信用返済で bot の建玉を特定するのに使う）
                    opened = orders.last_open_order(session, strat_row.id, symbol_code)
                    o_type = opened.trade_type if opened else "cash"
                    m_type = opened.margin_type if opened else 0
                    open_date = orders.jst_yyyymmdd(opened.ts) if opened else 0
                orders.queue_order(
                    session, strat_row, symbol_code, side, qty_hint, reason,
                    idempotency_key=key, ref_price=price,
                    trade_type=o_type, margin_type=m_type if o_type == "margin" else 0,
                    open_date=open_date,
                )
                if side in CLOSE_SIDES:
                    pnl = _estimate_pnl(pos, price)
                # 実際に受理されたかは bridge の報告待ちだが、同一足内で矛盾したシグナルを
                # 出さないよう楽観的にポジションを進めておく（ブロックされた場合は進めない）。
                pos = _advance(pos, side, qty_hint, price)
        else:
            if side in CLOSE_SIDES:
                pnl = _estimate_pnl(pos, price)
            pos = _advance(pos, side, qty_hint, price)
        if notify:
            send(format_signal(strat_row.name, symbol_code, name, side, price, signal_reason, pnl=pnl))

    cur.last_bar_ts = latest_ts
    cur.updated_at = utcnow()
    session.add(cur)
    session.commit()
    return fired


def _estimate_pnl(pos: Position, price: float) -> dict | None:
    """手仕舞いの損益の概算（建値＝建てたシグナルの価格、手仕舞い値＝今回のシグナルの価格）。"""
    if pos.is_flat or not pos.avg_price:
        return None
    qty = abs(pos.qty)
    return {
        "pnl": (price - pos.avg_price) * qty * pos.direction,
        "return_pct": (price / pos.avg_price - 1) * 100 * pos.direction,
        "entry": pos.avg_price, "exit": price, "qty": qty, "short": pos.is_short, "estimate": True,
    }


def _advance(pos: Position, side: str, qty: int, price: float) -> Position:
    if side in OPEN_SIDES and pos.is_flat:
        return Position(qty=qty * OPEN_SIDES[side], avg_price=price)
    if side in CLOSE_SIDES and not pos.is_flat:
        return Position()
    return pos


def run_once(session: Session, *, notify: bool = True, now: datetime | None = None) -> list[Signal]:
    """enabled な全 Strategy を1周評価する。生成した live Signal を返す。

    now（naive UTC、既定は現在時刻）は mode=live の古いシグナル判定に使う。テスト用。
    """
    strategies = session.exec(select(Strategy).where(Strategy.enabled == True)).all()  # noqa: E712
    out: list[Signal] = []
    for strat_row in strategies:
        try:
            cls = load_strategy_class(strat_row.class_path)
            params = json.loads(strat_row.params_json or "{}")
        except Exception:  # noqa: BLE001
            log.exception("戦略ロード失敗: %s (%s)", strat_row.name, strat_row.class_path)
            continue
        strat = cls(params)
        strat.timeframe = strat_row.timeframe
        for symbol_code in _symbols_for(session, strat_row):
            try:
                out.extend(_run_strategy_symbol(
                    session, strat_row, strat, symbol_code, notify=notify, now=now
                ))
            except Exception:  # noqa: BLE001
                log.exception("live 評価失敗: %s / %s", strat_row.name, symbol_code)
    return out
