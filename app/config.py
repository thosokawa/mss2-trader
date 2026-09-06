"""設定ロード。config/config.toml が無ければ config.example.toml をフォールバックに使う。"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"


@dataclass
class AppCfg:
    db_url: str = "sqlite:///data/mss2.db"
    timezone: str = "Asia/Tokyo"
    # tick -> 足 の集約を回す間隔（秒）。0 でバックグラウンド集約を無効化。
    agg_interval_sec: float = 15.0
    # 集約する足種
    agg_timeframes: list[str] = field(default_factory=lambda: ["1m", "5m"])


@dataclass
class NotifyCfg:
    slack_webhook_url: str = ""
    dry_run: bool = True


@dataclass
class BridgeCfg:
    ingest_url: str = "http://127.0.0.1:8000/api/ingest"
    workbook_path: str = ""
    poll_interval_sec: float = 2.0


@dataclass
class TradingCfg:
    enabled: bool = False
    max_qty_per_order: int = 100
    max_notional_per_order: int = 300_000
    daily_loss_limit: int = 30_000
    session_windows: list[str] = field(default_factory=lambda: ["09:00-11:30", "12:30-15:30"])


@dataclass
class Config:
    app: AppCfg
    notify: NotifyCfg
    bridge: BridgeCfg
    trading: TradingCfg


def _load_toml() -> dict:
    for name in ("config.toml", "config.example.toml"):
        p = CONFIG_DIR / name
        if p.exists():
            return tomllib.loads(p.read_text("utf-8"))
    return {}


@lru_cache
def get_config() -> Config:
    raw = _load_toml()
    app_raw = dict(raw.get("app", {}))
    if os.environ.get("MSS2_DB_URL"):
        app_raw["db_url"] = os.environ["MSS2_DB_URL"]
    return Config(
        app=AppCfg(**app_raw),
        notify=NotifyCfg(**raw.get("notify", {})),
        bridge=BridgeCfg(**raw.get("bridge", {})),
        trading=TradingCfg(**raw.get("trading", {})),
    )
