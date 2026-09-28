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


def format_signal(strategy_name: str, symbol: str, name: str, side: str, price: float, reason: str) -> str:
    icon = {
        "BUY": "🟢 買い", "SELL": "🔴 売り", "SHORT": "🔴 売建", "EXIT": "⚪ 手仕舞い", "COVER": "⚪ 買戻し",
    }.get(side, side)
    return f"*{icon}* {symbol} {name}  @{price:,.1f}\n戦略: {strategy_name}\n根拠: {reason}"
