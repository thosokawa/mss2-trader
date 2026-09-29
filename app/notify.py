"""シグナル通知（Slack Incoming Webhook / メール）。dry_run のときは送信せずログ出力のみ。

send() が設定済みの経路すべてに送る。メールは Gmail の SMTP（STARTTLS, 587）を想定し、
config の email_to / smtp_user / smtp_password がすべて入っているときだけ送る。
"""
from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

import httpx

from app.config import get_config

log = logging.getLogger("notify")


def send(text: str) -> bool:
    """設定済みの全経路（Slack / メール）へ送る。1つでも失敗したら False。"""
    cfg = get_config().notify
    if cfg.dry_run or not (cfg.slack_webhook_url or _email_configured()):
        log.info("[notify dry_run] %s", text)
        return True
    ok = True
    if cfg.slack_webhook_url:
        ok = send_slack(text) and ok
    if _email_configured():
        ok = send_email(text) and ok
    return ok


def send_slack(text: str) -> bool:
    cfg = get_config().notify
    if cfg.dry_run or not cfg.slack_webhook_url:
        log.info("[notify dry_run] %s", text)
        return True
    try:
        r = httpx.post(cfg.slack_webhook_url, json={"text": text}, timeout=10)
        r.raise_for_status()
        return True
    except Exception as e:  # noqa: BLE001
        log.error("slack 送信失敗: %s", e)
        return False


def _email_configured() -> bool:
    cfg = get_config().notify
    return bool(cfg.email_to.strip() and cfg.smtp_user and cfg.smtp_password)


def send_email(text: str, subject: str | None = None) -> bool:
    """text の1行目（Slack 用の * 強調は外す）を件名にしてメール送信する。"""
    cfg = get_config().notify
    plain = text.replace("*", "")
    if cfg.dry_run or not _email_configured():
        log.info("[notify dry_run] %s", plain)
        return True
    msg = EmailMessage()
    msg["Subject"] = subject or f"[mss2] {plain.splitlines()[0]}"
    msg["From"] = cfg.smtp_user
    msg["To"] = ", ".join(a.strip() for a in cfg.email_to.split(",") if a.strip())
    msg.set_content(plain)
    try:
        with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=15) as smtp:
            smtp.starttls()
            smtp.login(cfg.smtp_user, cfg.smtp_password)
            smtp.send_message(msg)
        return True
    except Exception as e:  # noqa: BLE001
        log.error("メール送信失敗: %s", e)
        return False


def format_signal(
    strategy_name: str,
    symbol: str,
    name: str,
    side: str,
    price: float,
    reason: str,
    pnl: dict | None = None,
) -> str:
    """シグナル通知の本文。手仕舞いなら pnl（format_pnl の引数）で損益の行を足す。"""
    icon = {
        "BUY": "🟢 買い", "SELL": "🔴 売り", "SHORT": "🔴 売建", "EXIT": "⚪ 手仕舞い", "COVER": "⚪ 買戻し",
    }.get(side, side)
    head = f"*{icon}* {symbol} {name}  @{price:,.1f}"
    if pnl:  # 1行目はメールの件名になるので、損益の金額も入れておく
        head += f"  {'+' if pnl['pnl'] >= 0 else '-'}{abs(pnl['pnl']):,.0f}円"
    text = f"{head}\n戦略: {strategy_name}\n根拠: {reason}"
    if pnl:
        text += "\n" + format_pnl(**pnl)
    return text


def format_pnl(
    pnl: float, return_pct: float, entry: float, exit: float, qty: int, short: bool = False,
    estimate: bool = False,
) -> str:
    """例: 損益: +1,200円（+0.20%）建値 6,110.0 → 6,122.0 × 100株 買建"""
    sign = "+" if pnl >= 0 else "-"
    mark = "🔺" if pnl > 0 else "🔻" if pnl < 0 else ""
    note = "（概算: シグナル時点の価格で計算）" if estimate else ""
    return (
        f"損益: {mark}{sign}{abs(pnl):,.0f}円（{return_pct:+.2f}%）"
        f" 建値 {entry:,.1f} → {exit:,.1f} × {qty}株 {'売建' if short else '買建'}{note}"
    )
