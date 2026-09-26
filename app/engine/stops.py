"""損切り・利確（ストップ / ターゲット）判定。バックテストと live 両方から使う共通ロジック。

戦略パラメータの `stop_loss_pct` / `take_profit_pct`（建値からの%、どちらも省略可・
`registry.builtin_params()` が全戦略に共通で持たせる）を見て、その足の高値・安値が
ラインに達していないかを判定する。同じ足で両方に達した場合は保守的にストップを優先する
（利食いより損切りを信じる）。

現状の制約: 判定は「その足の高値/安値」だけを見る（板・寄り付き前の跳ねなどは考慮しない）。
成行で建値からの%で即決済する想定で、指値の概念は無い。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class StopTargetHit:
    price: float
    reason: str


def _to_positive_float(v: object) -> float | None:
    if v is None or v == "":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def check_stop_target(
    avg_price: float,
    bar_high: float,
    bar_low: float,
    *,
    stop_loss_pct: object = None,
    take_profit_pct: object = None,
) -> StopTargetHit | None:
    if avg_price <= 0:
        return None
    sl = _to_positive_float(stop_loss_pct)
    tp = _to_positive_float(take_profit_pct)
    if sl is not None:
        stop_price = avg_price * (1 - sl / 100)
        if bar_low <= stop_price:
            return StopTargetHit(stop_price, f"損切り -{sl:.1f}%")
    if tp is not None:
        target_price = avg_price * (1 + tp / 100)
        if bar_high >= target_price:
            return StopTargetHit(target_price, f"利確 +{tp:.1f}%")
    return None
