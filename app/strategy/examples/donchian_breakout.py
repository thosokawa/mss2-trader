"""ドンチャンチャネル・ブレイクアウト（タートル風のトレンドフォロー）。

移動平均を見るのではなく、直近 N 本の高値を上抜けたら「新しい上昇の始まり」として
飛び乗る。手仕舞いは別の（短い）期間の安値割れ — エントリーより早く反応させ、
利益を早めに確保しにいく定番の型。
"""
from __future__ import annotations

from app.strategy.base import Context, Signal, Strategy
from app.strategy.indicators import donchian_lower, donchian_upper


class DonchianBreakout(Strategy):
    timeframe = "5m"
    description = "直近N本の高値ブレイクで買い、より短いM本の安値割れで手仕舞い"
    default_params = {
        "entry_period": 20,
        "exit_period": 10,
        "qty": 100,
    }
    param_meta = {
        "entry_period": {"label": "エントリー判定期間", "help": "この本数の高値をブレイクしたら買い"},
        "exit_period": {
            "label": "手仕舞い判定期間",
            "help": "この本数の安値を割ったら手仕舞い（entry_periodより短めが定番）",
        },
        "qty": {"label": "株数", "help": "1回のエントリーで売買する株数"},
    }

    def on_bar(self, ctx: Context) -> Signal | None:
        p = self.params
        entry_n = int(p["entry_period"])
        exit_n = int(p["exit_period"])
        bars = ctx.bars
        if len(bars) < max(entry_n, exit_n) + 2:
            return None

        price = float(bars["close"].iloc[-1])

        if ctx.position.is_flat:
            upper = donchian_upper(bars["high"], entry_n)
            if price > float(upper.iloc[-1]):
                return Signal(
                    "BUY",
                    int(p["qty"]),
                    reason=f"{entry_n}本高値ブレイク 終値{price:.1f}>{upper.iloc[-1]:.1f}",
                )
            return None

        if ctx.position.is_long:
            lower_exit = donchian_lower(bars["low"], exit_n)
            if price < float(lower_exit.iloc[-1]):
                return Signal(
                    "EXIT",
                    reason=f"{exit_n}本安値割れ 終値{price:.1f}<{lower_exit.iloc[-1]:.1f}",
                )
        return None
