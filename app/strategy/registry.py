"""class_path 文字列（"module:ClassName"）から Strategy クラスを解決する。"""
from __future__ import annotations

import importlib

from app.strategy.base import Strategy

# UI のプルダウン用の既知ストラテジー一覧
BUILTIN = {
    "SMAクロス": "app.strategy.examples.sma_cross:SmaCross",
    "移動平均+RSI": "app.strategy.examples.ma_rsi:MaRsi",
    "トレンド×RSI出戻り": "app.strategy.examples.trend_rsi_reclaim:TrendRsiReclaim",
}


def load_strategy_class(class_path: str) -> type[Strategy]:
    module_name, _, cls_name = class_path.partition(":")
    if not cls_name:
        raise ValueError(f"class_path は 'module:ClassName' 形式: {class_path!r}")
    mod = importlib.import_module(module_name)
    cls = getattr(mod, cls_name)
    if not issubclass(cls, Strategy):
        raise TypeError(f"{class_path} は Strategy のサブクラスではありません")
    return cls


def builtin_params() -> dict[str, dict]:
    """class_path -> default_params。UI のパラメータ欄の初期値に使う。"""
    out: dict[str, dict] = {}
    for cp in BUILTIN.values():
        try:
            out[cp] = dict(load_strategy_class(cp).default_params)
        except Exception:  # noqa: BLE001
            out[cp] = {}
    return out
