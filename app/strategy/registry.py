"""class_path 文字列（"module:ClassName"）から Strategy クラスを解決する。"""
from __future__ import annotations

import importlib

from app.strategy.base import Strategy

# UI のプルダウン用の既知ストラテジー一覧
BUILTIN = {
    "SMAクロス": "app.strategy.examples.sma_cross:SmaCross",
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
