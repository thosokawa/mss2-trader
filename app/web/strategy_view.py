"""戦略の設定を人が読める形にする（一覧の要約文・詳細画面の設定表）。

params_json の生の JSON ではなく、戦略クラスの param_meta（日本語ラベル・説明）と
全戦略共通パラメータ（registry.UNIVERSAL_META）を使って「短期期間 12」のように表示する。
"""
from __future__ import annotations

import json

from app.models import BacktestRun, Strategy
from app.strategy.base import exit_rule_of
from app.strategy.registry import BUILTIN, UNIVERSAL_DEFAULTS, builtin_param_meta, builtin_params

LOGIC_LABELS = {cp: label for label, cp in BUILTIN.items()}
MODE_LABELS = {"notify": "通知のみ", "paper": "ペーパー", "live": "実発注"}
DIRECTION_LABELS = {"long": "買いのみ", "short": "売りのみ", "both": "買い・売り"}
LEGACY_KEYS = ("allow_short",)  # 旧パラメータ（direction に置き換え済み）


def load_params(st: Strategy | BacktestRun) -> dict:
    try:
        v = json.loads(st.params_json or "{}")
        return v if isinstance(v, dict) else {}
    except ValueError:
        return {}


def _fmt(v) -> str:
    if v is None or v == "":
        return "なし"
    if isinstance(v, bool):
        return "ON" if v else "OFF"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def _pct(v) -> str | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f"{f:g}%" if f > 0 else None


def describe(st: Strategy | BacktestRun) -> dict:
    """{logic, summary, logic_params: [(label, value, help)], common: [(label, value, help)], ...}

    バックテスト履歴（BacktestRun: class_path/params_json/timeframe を持ち mode は無い）にも使う。
    """
    params = load_params(st)
    cp = st.class_path
    defaults = builtin_params().get(cp, {})
    meta = builtin_param_meta().get(cp, {})
    merged = {**defaults, **params}

    logic_params = []
    for key in defaults:
        if key in UNIVERSAL_DEFAULTS or key == "qty":
            continue
        m = meta.get(key, {})
        logic_params.append((m.get("label", key), _fmt(merged.get(key)), m.get("help", "")))
    # 既定値に無いキー（手で JSON に足したもの等）も落とさず出す
    for key, v in params.items():
        if key not in defaults and key not in UNIVERSAL_DEFAULTS and key not in LEGACY_KEYS:
            logic_params.append((key, _fmt(v), ""))

    direction = merged.get("direction") or ("both" if merged.get("allow_short") else "long")
    trade_type = merged.get("trade_type") or "cash"
    hold = merged.get("hold_overnight", True)
    if isinstance(hold, str):
        hold = hold.strip().lower() not in ("false", "0", "no", "off", "")
    stop, take = _pct(merged.get("stop_loss_pct")), _pct(merged.get("take_profit_pct"))
    qty = merged.get("qty", 100)
    exit_rule = exit_rule_of(merged)
    exit_fast, exit_slow = _fmt(merged.get("exit_sma_fast") or 10), _fmt(merged.get("exit_sma_slow") or 30)
    exit_text = (f"SMAクロス（{exit_fast}/{exit_slow}）" if exit_rule == "sma_cross"
                 else "エントリー条件の反転シグナル")

    common = [
        ("株数", f"{_fmt(qty)} 株", "1回のエントリーで売買する株数"),
        ("売買方向", DIRECTION_LABELS.get(direction, direction), ""),
        ("取引区分", "信用" if trade_type == "margin" else "現物", ""),
        ("大引けをまたぐ", "またぐ（持ち越す）" if hold else "またがない（大引け前に手仕舞い）", ""),
        ("決済条件", exit_text, "損切り・利確・大引け手仕舞いはこれとは別に効く"),
        ("損切り", stop or "なし", "建値からこの%逆行したら成行で手仕舞い"),
        ("利確", take or "なし", "建値からこの%進んだら成行で手仕舞い"),
    ]

    logic = LOGIC_LABELS.get(cp, cp.rsplit(":", 1)[-1])
    # 要約には数値のパラメータだけ出す（ON/OFF や選択肢は何のことか分かりにくいので詳細画面で）
    nums = [v for _, v, _ in logic_params if v.replace(".", "", 1).lstrip("-").isdigit()]
    short_vals = "/".join(nums[:4])
    parts = [f"{logic}（{short_vals}）" if short_vals else logic, st.timeframe,
             DIRECTION_LABELS.get(direction, direction), "信用" if trade_type == "margin" else "現物"]
    if not hold:
        parts.append("デイトレ")
    if exit_rule == "sma_cross":
        parts.append(f"決済SMA{exit_fast}/{exit_slow}")
    if stop:
        parts.append(f"損切り{stop}")
    if take:
        parts.append(f"利確{take}")
    parts.append(f"{_fmt(qty)}株")
    return {
        "logic": logic,
        "class_path": cp,
        "summary": "・".join(parts),
        "logic_params": logic_params,
        "common": common,
        "mode_label": MODE_LABELS.get(getattr(st, "mode", ""), getattr(st, "mode", "")),
        "direction": direction,
        "trade_type": trade_type,
        "hold_overnight": hold,
    }
