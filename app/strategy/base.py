"""売買ロジックの共通インターフェース。

live.py（本番）と backtest.py（検証）は同じ Strategy.decide()（= on_bar() + 売買方向の絞り込み）を呼ぶ。
これにより「バックテストで良かったロジックが本番で別物になる」事故を防ぐ。

ロジック作者が書くのは on_bar() だけ。1本の足が確定するたびに呼ばれ、
売買したいときだけ Signal を返す（何もしないときは None）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta

import pandas as pd

from app.strategy.indicators import TIMEFRAME_MINUTES

EXIT_RULES = ("signal", "sma_cross", "both")
CLOSE_SIDES = ("EXIT", "SELL", "COVER")


def exit_rule_of(params: dict) -> str:
    """全戦略共通パラメータ exit_rule（決済条件）:
    "signal"=エントリー条件の反転シグナル（on_bar の手仕舞い。既定）/ "sma_cross"=SMAクロス /
    "both"=反転シグナルと SMAクロスのどちらか早い方。
    """
    v = str(params.get("exit_rule") or "").strip().lower()
    return v if v in EXIT_RULES else "signal"


_WINDOW_RE = re.compile(r"(\d{1,2}):?(\d{2})\s*[-~〜～]\s*(\d{1,2}):?(\d{2})")


def parse_entry_windows(text) -> list[tuple[time, time]]:
    """全戦略共通パラメータ entry_windows（エントリー時間帯、JST）を [(開始, 終了)] に。

    "9:00-10:00 13:30-14:30" のように空白（または ; 、 / ・ ,）で複数。空欄なら [] ＝ 終日。
    書式が読めない部分は無視する。
    """
    out = []
    for m in _WINDOW_RE.finditer(str(text or "")):
        h1, m1, h2, m2 = (int(x) for x in m.groups())
        if h1 < 24 and h2 < 24 and m1 < 60 and m2 < 60:
            out.append((time(h1, m1), time(h2, m2)))
    return out


@dataclass
class Position:
    """建玉。qty > 0 が買い持ち（ロング）、qty < 0 が売り持ち（ショート＝信用の売建）。"""

    qty: int = 0
    avg_price: float = 0.0

    @property
    def is_flat(self) -> bool:
        return self.qty == 0

    @property
    def is_long(self) -> bool:
        return self.qty > 0

    @property
    def is_short(self) -> bool:
        return self.qty < 0

    @property
    def direction(self) -> int:
        """+1=ロング / -1=ショート / 0=ノーポジ。損益は (価格差) × |qty| × direction。"""
        return (self.qty > 0) - (self.qty < 0)


@dataclass
class Signal:
    # "BUY"   : 新規買い（ロング）
    # "SHORT" : 新規売り（ショート＝信用の売建。self.allow_short のときだけ出す）
    # "EXIT"  : 今の建玉を手仕舞う（ロングなら売り、ショートなら買い戻し）
    # "SELL"  : 旧来の書き方。ロングの手仕舞いとして扱う（EXIT と同じ）
    side: str
    qty: int | None = None  # None なら戦略パラメータ / リスク層が決める
    reason: str = ""
    order_type: str = "MKT"  # "MKT" | "LMT"
    limit_price: float | None = None
    # 損切りとして出す手仕舞い（例: トレンド×RSI出戻りの「RSIの出戻り失敗」）。決済条件が SMAクロス でも
    # 止めない（決済条件が止めるのは「反転シグナル」の手仕舞いだけ）
    is_stop: bool = False


@dataclass
class Context:
    symbol: str
    now: datetime
    bars: pd.DataFrame  # index=ts, 列 open/high/low/close/volume。最終行が確定した最新足。
    position: Position
    params: dict = field(default_factory=dict)

    @property
    def price(self) -> float:
        return float(self.bars["close"].iloc[-1])

    @property
    def close(self) -> pd.Series:
        return self.bars["close"]


class Strategy:
    timeframe: str = "5m"
    default_params: dict = {}

    def __init__(self, params: dict | None = None):
        self.params = {**self.default_params, **(params or {})}

    @property
    def direction(self) -> str:
        """全戦略共通パラメータ direction（売買方向）: "long"=買いのみ / "short"=売りのみ / "both"=両方。

        旧パラメータ allow_short（空売りする、bool）だけが保存されている戦略は
        true → "both"、それ以外 → "long" として読む（既定は "long"＝従来どおり買いのみ）。
        """
        v = str(self.params.get("direction") or "").strip().lower()
        if v in ("long", "short", "both"):
            return v
        legacy = self.params.get("allow_short", False)
        if isinstance(legacy, str):
            legacy = legacy.strip().lower() in ("true", "1", "yes", "on")
        return "both" if legacy else "long"

    @property
    def allow_short(self) -> bool:
        """on_bar で SHORT（売建）を出してよいか（direction が short / both）。"""
        return self.direction in ("short", "both")

    @property
    def allow_long(self) -> bool:
        """BUY（新規買い）を出してよいか（direction が long / both）。"""
        return self.direction in ("long", "both")

    @property
    def exit_rule(self) -> str:
        return exit_rule_of(self.params)

    def _sma_cross_exit(self, ctx: Context) -> Signal | None:
        """決済条件=SMAクロス: 買建は短期SMAが長期SMAを下抜けた足、売建は上抜けた足で手仕舞う。"""
        fast_n = int(self.params.get("exit_sma_fast") or 10)
        slow_n = int(self.params.get("exit_sma_slow") or 30)
        c = ctx.close
        if len(c) < max(fast_n, slow_n) + 1:
            return None
        fast = c.rolling(fast_n).mean()
        slow = c.rolling(slow_n).mean()
        prev_diff = float(fast.iloc[-2] - slow.iloc[-2])
        diff = float(fast.iloc[-1] - slow.iloc[-1])
        tag = f"SMA{fast_n}/{slow_n}"
        if ctx.position.is_long and prev_diff >= 0 > diff:
            return Signal("EXIT", reason=f"決済: {tag} デッドクロス")
        if ctx.position.is_short and prev_diff <= 0 < diff:
            return Signal("EXIT", reason=f"決済: {tag} ゴールデンクロス")
        return None

    def decide(self, ctx: Context) -> Signal | None:
        """エンジン（backtest / live）が呼ぶ入口。on_bar の結果から売買方向に合わない
        新規建てを捨てる（手仕舞いは常に通す）。各戦略は BUY/SHORT を気にせず書いてよい。

        決済条件（exit_rule）が SMAクロス / 両方 なら、建玉があるときは SMA のクロスで手仕舞う。
        SMAクロス だけのときは on_bar が出す反転シグナルの手仕舞い（EXIT/SELL/COVER）は使わない
        （ただし損切りとして出した手仕舞い＝Signal.is_stop は通す）。損切り%・利確%・大引け手仕舞いは
        エンジン側（stops.py / eod.py）なので決済条件に関係なく効く。どれかに当たった時点で手仕舞う。
        """
        rule = self.exit_rule
        if rule in ("sma_cross", "both") and not ctx.position.is_flat:
            hit = self._sma_cross_exit(ctx)
            if hit is not None:
                return hit
        sig = self.on_bar(ctx)
        if sig is None:
            return None
        if (rule == "sma_cross" and not ctx.position.is_flat and sig.side in CLOSE_SIDES
                and not sig.is_stop):
            return None
        if sig.side == "BUY" and not self.allow_long:
            return None
        if sig.side == "SHORT" and not self.allow_short:
            return None
        if sig.side in ("BUY", "SHORT") and not self.in_entry_window(ctx):
            return None
        return sig

    def gap_direction(self, ctx: Context) -> int:
        """当日のギャップの向き: +1=ギャップアップ（始値＞前日終値）/ -1=ギャップダウン /
        0=なし・前日の足が無い。

        各戦略の「ギャップの向きにだけ建てる」（gap_filter）用。日付は JST。
        1日1回だけ計算して使い回す（バックテストで毎足計算すると遅いため）。
        """
        idx = ctx.bars.index
        if len(idx) == 0:
            return 0
        jst = pd.Timedelta(hours=9)
        day = (pd.Timestamp(idx[-1]) + jst).normalize()
        cache = getattr(self, "_gap_cache", None)
        if cache and cache[0] == day:
            return cache[1]
        today = (idx + jst).normalize() == day
        gap = 0
        if today.any() and not today.all():
            first = int(today.argmax())
            if first > 0:
                prev_close = float(ctx.bars["close"].iloc[first - 1])
                today_open = float(ctx.bars["open"].iloc[first])
                gap = (today_open > prev_close) - (today_open < prev_close)
        self._gap_cache = (day, gap)
        return gap

    def _truthy_param(self, key: str, default: bool = False) -> bool:
        v = self.params.get(key, default)
        return v.strip().lower() not in ("false", "0", "no", "off", "") if isinstance(v, str) else bool(v)

    def gap_allows(self, ctx: Context) -> tuple[bool, bool]:
        """(買ってよいか, 売建ててよいか)。パラメータ gap_filter が ON のときだけ、ギャップの向きで絞る。"""
        v = self.params.get("gap_filter", False)
        on = v.strip().lower() not in ("false", "0", "no", "off", "") if isinstance(v, str) else bool(v)
        if not on:
            return True, True
        gap = self.gap_direction(ctx)
        return gap >= 0, gap <= 0

    def in_entry_window(self, ctx: Context) -> bool:
        """エントリー時間帯（entry_windows）の中か。判定は足が確定した時刻（＝発注する時刻）の JST。
        時間帯が空欄なら終日 True。日足は時刻が無いので常に True。手仕舞いはこれに関係なく出す。"""
        windows = parse_entry_windows(self.params.get("entry_windows"))
        minutes = TIMEFRAME_MINUTES.get(self.timeframe, 5)
        if not windows or minutes >= 1440 or ctx.bars.empty:
            return True
        t = (pd.Timestamp(ctx.bars.index[-1]) + timedelta(hours=9, minutes=minutes)).time()
        return any(a <= t <= b for a, b in windows)

    def on_bar(self, ctx: Context) -> Signal | None:  # pragma: no cover - 抽象
        raise NotImplementedError

    # ロジック作者向けの説明文（UI に表示）
    description: str = ""

    # UI がパラメータ入力欄を組み立てるためのヒント（無くても動く。省略したキーは
    # 項目名がそのまま表示される）。例:
    #   {"fast": {"label": "短期期間", "help": "短期移動平均の本数"},
    #    "ma_type": {"label": "種類", "choices": ["ema", "sma"]}}
    param_meta: dict[str, dict] = {}
