"""Slack Incoming Webhook 通知。dry_run のときは送信せずログ出力のみ。"""
from __future__ import annotations

import logging

import httpx

from app.config import get_config

log = logging.getLogger("notify")


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


def format_signal(strategy_name: str, symbol: str, name: str, side: str, price: float, reason: str) -> str:
    icon = {"BUY": "🟢 買い", "SELL": "🔴 売り", "EXIT": "⚪ 手仕舞い"}.get(side, side)
    return f"*{icon}* {symbol} {name}  @{price:,.1f}\n戦略: {strategy_name}\n根拠: {reason}"
