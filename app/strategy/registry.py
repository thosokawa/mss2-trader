"""class_path 文字列（"module:ClassName"）から Strategy クラスを解決する。"""
from __future__ import annotations

import importlib

from app.strategy.base import Strategy

# UI のプルダウン用の既知ストラテジー一覧
BUILTIN = {
    "SMAクロス": "app.strategy.examples.sma_cross:SmaCross",
    "移動平均+RSI": "app.strategy.examples.ma_rsi:MaRsi",
    "トレンド×RSI出戻り": "app.strategy.examples.trend_rsi_reclaim:TrendRsiReclaim",
    "MACDクロス": "app.strategy.examples.macd_cross:MacdCross",
    "ボリンジャー逆張り": "app.strategy.examples.bollinger_reversion:BollingerReversion",
    "ドンチャンブレイクアウト": "app.strategy.examples.donchian_breakout:DonchianBreakout",
    "ADXフィルタ+MAクロス": "app.strategy.examples.adx_ma_cross:AdxMaCross",
}


# どの戦略にも共通で持たせるリスク管理パラメータ（損切り/利確）。
# app/engine/stops.py が strategy.params から読む。既定は None＝無効。
UNIVERSAL_DEFAULTS = {"stop_loss_pct": None, "take_profit_pct": None}
UNIVERSAL_META = {
    "stop_loss_pct": {
        "label": "損切り(%)", "type": "number",
        "help": "建値からこの%下落したら成行で手仕舞い（on_barの判断より優先）。空欄で無効",
    },
    "take_profit_pct": {
        "label": "利確(%)", "type": "number",
        "help": "建値からこの%上昇したら成行で手仕舞い（on_barの判断より優先）。空欄で無効",
    },
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
    """class_path -> default_params（+ 損切り/利確を全戦略共通で追加）。UI の入力欄の初期値に使う。"""
    out: dict[str, dict] = {}
    for cp in BUILTIN.values():
        try:
            out[cp] = {**dict(load_strategy_class(cp).default_params), **UNIVERSAL_DEFAULTS}
        except Exception:  # noqa: BLE001
            out[cp] = dict(UNIVERSAL_DEFAULTS)
    return out


def builtin_param_meta() -> dict[str, dict]:
    """class_path -> param_meta（+ 損切り/利確の説明を全戦略共通で追加）。"""
    out: dict[str, dict] = {}
    for cp in BUILTIN.values():
        try:
            out[cp] = {**dict(load_strategy_class(cp).param_meta), **UNIVERSAL_META}
        except Exception:  # noqa: BLE001
            out[cp] = dict(UNIVERSAL_META)
    return out
