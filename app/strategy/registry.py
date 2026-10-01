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
    "ギャップ後の高値更新": "app.strategy.examples.gap_breakout:GapBreakout",
}


# どの戦略にも共通で持たせるパラメータ（損切り/利確/大引けをまたぐか/売買方向/取引区分/決済条件）。
# app/engine/stops.py・eod.py・live.py 等が strategy.params から読む。既定は従来どおりの挙動
# （損切り/利確は None＝無効、hold_overnight は True＝持ち越す、空売りしない、現物）。
# 並び順がそのまま入力欄の順（戦略固有のパラメータ → 株数 のあと）
UNIVERSAL_DEFAULTS = {
    "direction": "long",
    "trade_type": "cash",
    "hold_overnight": True,
    "entry_windows": "",
    "exit_rule": "signal",
    "exit_sma_fast": 10,
    "exit_sma_slow": 30,
    "stop_loss_pct": None,
    "take_profit_pct": None,
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

UNIVERSAL_META["entry_windows"] = {
    "label": "エントリー時間帯", "type": "text", "wide": True,
    "placeholder": "空欄=終日　例 9:00-10:00 13:30-14:30",
    "help": "この時間帯（JST、足が確定して発注する時刻）だけ新規に建てる。複数は空白区切り。"
            "空欄なら終日。手仕舞い・損切り・大引け手仕舞いは時間帯に関係なく出る。"
            "最適化ではカンマで候補を区切る（例 9:00-11:30, 9:00-10:00 13:30-14:30）",
}
UNIVERSAL_META["exit_rule"] = {
    "label": "決済条件", "choices": ["signal", "sma_cross"], "wide": True,
    "choice_labels": {"signal": "エントリー条件の反転シグナル", "sma_cross": "SMAクロス"},
    "help": "反転シグナル: 戦略のエントリー条件の逆が出たら手仕舞い（従来どおり）。"
            "SMAクロス: 買建は短期SMAが長期SMAを下抜けたら、売建は上抜けたら手仕舞い"
            "（戦略の手仕舞いシグナルは使わない）。"
            "損切り・利確・大引け手仕舞いはどちらでも効く",
}
UNIVERSAL_META["exit_sma_fast"] = {
    "label": "決済SMA短期", "type": "number", "show_if": {"exit_rule": "sma_cross"},
    "help": "決済条件=SMAクロスのときの短期SMAの本数",
}
UNIVERSAL_META["exit_sma_slow"] = {
    "label": "決済SMA長期", "type": "number", "show_if": {"exit_rule": "sma_cross"},
    "help": "決済条件=SMAクロスのときの長期SMAの本数",
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
            own = dict(load_strategy_class(cp).default_params)
            qty = {"qty": own.pop("qty")} if "qty" in own else {}
            # 入力欄の順: 戦略固有 → 株数 → 共通パラメータ
            out[cp] = {**own, **qty, **UNIVERSAL_DEFAULTS}
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
