"""チャート（ローソク足＋エントリー/決済の印＋移動平均）用のデータを作る。

描画はブラウザ側の static/chart.js（TradingView の Lightweight Charts）。ここは JSON を返すだけ。

- 時刻: DB は naive UTC。Lightweight Charts は時刻を UTC として表示するので、+9時間した値を
  UNIX 秒で渡して、軸・カーソルが JST で読めるようにする。
- 印の時刻は足の開始時刻に丸める（発注の時刻は足の途中なので、その時刻を含む足に付ける）。
- 色: 足・損益は 上げ/プラス=赤、下げ/マイナス=青。建ては 買い=赤・売り=青。
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

from app.strategy.indicators import ema, sma

JST = timedelta(hours=9)
TF_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "1d": 1440}

C_BUY = "#FF5A5A"
C_SHORT = "#5AA9FF"
C_UP = "#FF5A5A"
C_DN = "#5AA9FF"
C_MUTED = "#8591A2"


def to_time(ts: datetime) -> int:
    """naive UTC → チャートの時刻（JST の壁時計を UTC とみなした UNIX 秒）。"""
    return int((pd.Timestamp(ts) + JST).tz_localize("UTC").timestamp())


def floor_ts(ts: datetime, tf: str) -> datetime:
    minutes = TF_MINUTES.get(tf, 1)
    if minutes >= 1440:
        return pd.Timestamp(ts).normalize().to_pydatetime()
    return pd.Timestamp(ts).floor(f"{minutes}min").to_pydatetime()


def bars_payload(bars: pd.DataFrame) -> dict:
    if bars.empty:
        return {"candles": [], "volume": []}
    candles, volume = [], []
    for ts, r in bars.iterrows():
        t = to_time(ts)
        candles.append({"time": t, "open": float(r["open"]), "high": float(r["high"]),
                        "low": float(r["low"]), "close": float(r["close"])})
        up = r["close"] >= r["open"]
        volume.append({"time": t, "value": float(r.get("volume", 0) or 0),
                       "color": "rgba(255,90,90,.35)" if up else "rgba(90,169,255,.35)"})
    return {"candles": candles, "volume": volume}


def _pnl_text(pnl: float | None) -> str:
    if pnl is None:
        return "決済"
    v = round(pnl)
    return f"決済 {'+' if v > 0 else '−' if v < 0 else '±'}{abs(v):,}"


def entry_marker(ts: datetime, tf: str, side: str, price: float | None, label: str = "") -> dict:
    short = side in ("SHORT", "SELL_SHORT")
    return {
        "time": to_time(floor_ts(ts, tf)),
        "position": "aboveBar" if short else "belowBar",
        "color": C_SHORT if short else C_BUY,
        "shape": "arrowDown" if short else "arrowUp",
        "text": label or ("売" if short else "買"),
        "price": price,
    }


def exit_marker(ts: datetime, tf: str, was_short: bool, pnl: float | None, price: float | None,
                label: str = "") -> dict:
    color = C_MUTED if pnl is None else C_UP if pnl > 0 else C_DN if pnl < 0 else C_MUTED
    return {
        "time": to_time(floor_ts(ts, tf)),
        "position": "belowBar" if was_short else "aboveBar",
        "color": color,
        "shape": "circle",
        "text": label or _pnl_text(pnl),
        "price": price,
    }


def sort_markers(markers: list[dict]) -> list[dict]:
    return sorted(markers, key=lambda m: m["time"])


# ---- 戦略の移動平均をチャートに重ねる -------------------------------------------

MA_COLORS = ["#FFB020", "#C98AD8", "#7DBBFF", "#A3AEBD"]


def overlays(class_path: str, params: dict, bars: pd.DataFrame) -> list[dict]:
    """戦略が使っている移動平均（分かるものだけ）と、決済条件=SMAクロスの決済用SMA。"""
    if bars.empty:
        return []
    c = bars["close"].astype(float)
    lines: list[tuple[str, pd.Series]] = []

    def add(label: str, kind: str, n) -> None:
        try:
            n = int(float(n))
        except (TypeError, ValueError):
            return
        if n > 0:
            lines.append((f"{kind.upper()}{n}（{label}）", (ema if kind == "ema" else sma)(c, n)))

    name = class_path.rsplit(":", 1)[-1]
    if name == "TrendRsiReclaim":
        kind = str(params.get("ma_type", "sma")).lower()
        add("短期", kind, params.get("fast_period", 10))
        add("中期", kind, params.get("mid_period", 30))
    elif name == "SmaCross":
        add("短期", "sma", params.get("fast", 5))
        add("長期", "sma", params.get("slow", 20))
    if str(params.get("exit_rule", "")).lower() == "sma_cross":
        add("決済短期", "sma", params.get("exit_sma_fast", 10))
        add("決済長期", "sma", params.get("exit_sma_slow", 30))

    out = []
    for i, (label, s) in enumerate(lines):
        pts = [{"time": to_time(ts), "value": round(float(v), 2)} for ts, v in s.items() if pd.notna(v)]
        out.append({"name": label, "color": MA_COLORS[i % len(MA_COLORS)], "data": pts})
    return out
