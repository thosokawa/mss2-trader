"""ボリンジャーバンドの「拡大で順張り・縮小で逆張り」。

バンド幅（(上限−下限)÷中心線）で相場の状態を見分け、状態に合った売買をする。

- スクイーズ（縮小）: バンド幅が直近 bw_lookback 本の中で下位 squeeze_pctile % 以下
- エクスパンション（拡大）: バンド幅が前の足より広がっていて、直近 squeeze_memory 本以内にスクイーズがあった
  ＝縮小から広がり始めたところ

順張り（use_trend）: エクスパンション中に終値が上限を上抜け → 買い（下限を下抜け → 売り）。
  中心線を終値で割ったら（売りは上抜けたら）手仕舞い（バンドウォークが終わった）。
逆張り（use_reversion）: バンド幅が広がっていないとき、下限を割ってから終値で戻した足で買い
  （上限を超えてから戻した足で売り）。中心線に届いたら手仕舞い。
  建てた後にエクスパンションで逆方向に抜けたら（下限を再び下抜け）、トレンドに変わったので手仕舞い。

建玉が順張りか逆張りかは、建値が今の中心線より外側（買いなら上）なら順張り、内側なら逆張りとして扱う
（戦略は建玉の種類を覚えていられないため。順張りは上限の外、逆張りは下限の近くで建つので区別できる）。
"""
from __future__ import annotations

import numpy as np

from app.strategy.base import Context, Signal, Strategy
from app.strategy.indicators import bollinger_bands


def _truthy(v) -> bool:
    if isinstance(v, str):
        return v.strip().lower() not in ("false", "0", "no", "off", "")
    return bool(v)


class BollingerRegime(Strategy):
    timeframe = "5m"
    description = ("ボリンジャーのバンド幅で相場を判定。スクイーズ後の拡大で上限/下限ブレイクに順張り、"
                   "拡大していないときは下限/上限からの戻りに逆張り")
    default_params = {
        "period": 20,
        "num_std": 2.0,
        "bw_lookback": 100,
        "squeeze_pctile": 20.0,
        "squeeze_memory": 12,
        "use_trend": True,
        "use_reversion": True,
        "qty": 100,
    }
    param_meta = {
        "period": {"label": "期間"},
        "num_std": {"label": "バンド幅（標準偏差の倍率）"},
        "bw_lookback": {"label": "スクイーズ判定の本数", "help": "バンド幅をこの本数の中で比べる"},
        "squeeze_pctile": {"label": "スクイーズの基準(%)",
                           "help": "バンド幅が直近の中で下位この%以下ならスクイーズ（縮小）"},
        "squeeze_memory": {"label": "スクイーズからの本数",
                           "help": "この本数以内にスクイーズがあって、バンド幅が広がり始めたら"
                                   "エクスパンション"},
        "use_trend": {"label": "エクスパンションで順張り", "type": "bool"},
        "use_reversion": {"label": "拡大していないとき逆張り", "type": "bool"},
        "qty": {"label": "株数", "help": "1回のエントリーで売買する株数"},
    }

    def on_bar(self, ctx: Context) -> Signal | None:
        p = self.params
        n = int(p["period"])
        look = int(p["bw_lookback"])
        memory = int(p["squeeze_memory"])
        # 必要な分だけ後ろから使う（毎足の計算を一定の量に抑える）
        c = ctx.close.iloc[-(n + look + memory + 3):]
        if len(c) < n + look + memory + 2:
            return None
        mid, upper, lower = bollinger_bands(c, n, float(p["num_std"]))
        bw = ((upper - lower) / mid).to_numpy()
        price, prev = float(c.iloc[-1]), float(c.iloc[-2])
        m, up, lo = float(mid.iloc[-1]), float(upper.iloc[-1]), float(lower.iloc[-1])
        up_prev, lo_prev = float(upper.iloc[-2]), float(lower.iloc[-2])

        # スクイーズ: 各足のバンド幅が、その足までの look 本の中で下位 squeeze_pctile % 以下か
        q = float(p["squeeze_pctile"]) / 100.0

        def squeezed(j: int) -> bool:
            win = bw[j - look + 1: j + 1]
            return bool(np.mean(win <= bw[j]) <= q)

        last = len(bw) - 1
        recent_squeeze = any(squeezed(j) for j in range(last - memory, last))
        widening = bw[last] > bw[last - 1]
        expansion = widening and recent_squeeze
        pos = ctx.position
        tag = f"バンド幅{bw[last] * 100:.2f}%"

        if pos.is_long:
            trend_pos = pos.avg_price >= m
            if trend_pos and price < m:
                return Signal("EXIT", reason=f"中心線割れ（順張りの手仕舞い）終値{price:,.1f}<{m:,.1f}")
            if not trend_pos and price >= m:
                return Signal("EXIT", reason=f"中心線到達（逆張りの手仕舞い）終値{price:,.1f}")
            if not trend_pos and expansion and price < lo:
                return Signal("EXIT", reason=f"拡大して下限を下抜け（逆張り失敗）{tag}")
            return None
        if pos.is_short:
            trend_pos = pos.avg_price <= m
            if trend_pos and price > m:
                return Signal("EXIT", reason=f"中心線超え（順張りの手仕舞い）終値{price:,.1f}>{m:,.1f}")
            if not trend_pos and price <= m:
                return Signal("EXIT", reason=f"中心線到達（逆張りの手仕舞い）終値{price:,.1f}")
            if not trend_pos and expansion and price > up:
                return Signal("EXIT", reason=f"拡大して上限を上抜け（逆張り失敗）{tag}")
            return None

        qty = int(p["qty"])
        if _truthy(p.get("use_trend", True)) and expansion:
            if price > up and prev <= up_prev:
                return Signal("BUY", qty, reason=f"スクイーズ後の拡大で上限ブレイク {tag}")
            if price < lo and prev >= lo_prev:
                return Signal("SHORT", qty, reason=f"スクイーズ後の拡大で下限ブレイク {tag}")
        if _truthy(p.get("use_reversion", True)) and not widening:
            if prev < lo_prev and price >= lo:
                return Signal("BUY", qty, reason=f"下限からの戻り（逆張り）{tag}")
            if prev > up_prev and price <= up:
                return Signal("SHORT", qty, reason=f"上限からの戻り（逆張り）{tag}")
        return None
