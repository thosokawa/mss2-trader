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
    "MACDクロス＋上位足トレンド": "app.strategy.examples.macd_trend:MacdTrendFilter",
    "ボリンジャー逆張り": "app.strategy.examples.bollinger_reversion:BollingerReversion",
    "ドンチャンブレイクアウト": "app.strategy.examples.donchian_breakout:DonchianBreakout",
    "ADXフィルタ+MAクロス": "app.strategy.examples.adx_ma_cross:AdxMaCross",
}


# どの戦略にも共通で持たせるパラメータ（損切り/利確/大引けをまたぐか/空売り/取引区分）。
# app/engine/stops.py・eod.py・live.py 等が strategy.params から読む。既定は従来どおりの挙動
# （損切り/利確は None＝無効、hold_overnight は True＝持ち越す、空売りしない、現物）。
UNIVERSAL_DEFAULTS = {
    "stop_loss_pct": None,
    "take_profit_pct": None,
    "hold_overnight": True,
    "direction": "long",
    "trade_type": "cash",
}
UNIVERSAL_META = {
    "stop_loss_pct": {
        "label": "損切り(%)", "type": "number",
        "help": "建値からこの%下落したら成行で手仕舞い（on_barの判断より優先）。空欄で無効",
    },
    "take_profit_pct": {
        "label": "利確(%)", "type": "number",
        "help": "建値からこの%上昇したら成行で手仕舞い（on_barの判断より優先）。空欄で無効",
    },
    "hold_overnight": {
        "label": "大引けをまたぐ", "type": "bool",
        "help": "OFFにすると日中足(1m/5m/15m)では15:20までに確定する最後の足で成行手仕舞いし、"
                "その足では新規買いしない（デイトレ）。日足では無視",
    },
    "direction": {
        "label": "売買方向", "choices": ["long", "short", "both"],
        "choice_labels": {"long": "買いのみ", "short": "売りのみ", "both": "買い・売り両方"},
        # 買い=赤 / 売り=青 / 両方=半々（app.css の .pf-choice-*）
        "choice_classes": {"long": "pf-choice-long", "short": "pf-choice-short", "both": "pf-choice-both"},
        "help": "買いのみ / 売りのみ（買いと対称な条件で売建）/ 両方。売建の実発注には取引区分=信用が必要",
    },
    "trade_type": {
        "label": "取引区分", "choices": ["cash", "margin"],
        "choice_labels": {"cash": "現物", "margin": "信用"},
        # 選ばれている方で入力欄の色を変える（app.css の .pf-choice-*）。信用はリスクが大きいので目立つ色
        "choice_classes": {"cash": "pf-choice-cash", "margin": "pf-choice-margin"},
        "help": "実発注（モード=実発注）の注文種別。信用は、大引けをまたがないならいちにち信用、"
                "またぐなら一般信用・無期限。バックテスト・ペーパーの結果には影響しない",
    },
}

# 株数の入力欄: 上下の矢印で 100 株（国内株の売買単位）ずつ増減、最小 100
QTY_META = {"step": 100, "min": 100}


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
            cls = load_strategy_class(cp)
            meta = {**dict(cls.param_meta), **UNIVERSAL_META}
            if "qty" in cls.default_params:
                meta["qty"] = {**meta.get("qty", {}), **QTY_META}
            out[cp] = meta
        except Exception:  # noqa: BLE001
            out[cp] = dict(UNIVERSAL_META)
    return out
