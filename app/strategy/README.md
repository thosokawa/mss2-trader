# app/strategy/ — 売買ロジック

書くのは `Strategy.on_bar()` だけ。1本の確定足ごとに呼ばれ、売買したいときだけ
`Signal` を返す（何もしないときは `None`）。バックテスト（`engine/backtest.py`）と
ライブ（`engine/live.py`）が同じ `on_bar` を呼ぶので挙動が一致する。

## 最小の書き方

```python
from app.strategy.base import Context, Signal, Strategy
from app.strategy.indicators import ema, rsi

class MyStrategy(Strategy):
    timeframe = "5m"
    description = "UI に出る説明"
    default_params = {"ema_period": 25, "qty": 100}

    def on_bar(self, ctx: Context) -> Signal | None:
        c = ctx.close                       # 終値の Series（最終行が確定した最新足）
        if len(c) < 30:
            return None                     # ウォームアップ不足
        e = ema(c, self.params["ema_period"])
        if ctx.position.is_flat and c.iloc[-1] > e.iloc[-1]:
            return Signal("BUY", int(self.params["qty"]), reason="終値がEMA上")
        if ctx.position.is_long and c.iloc[-1] < e.iloc[-1]:
            return Signal("EXIT", reason="終値がEMA下")
        return None
```

登録は `registry.py` の `BUILTIN` に `"表示名": "module:ClassName"` を足す。
UI の「戦略」「バックテスト」画面のプルダウンに出る。

## Context で使えるもの

| | 中身 |
|---|---|
| `ctx.bars` | `DataFrame`（index=時刻, 列 `open/high/low/close/volume`）。最終行=確定した最新足 |
| `ctx.close` | `ctx.bars["close"]`（Series） |
| `ctx.price` | 最新足の終値（float） |
| `ctx.position` | `.is_flat` / `.is_long` / `.qty` / `.avg_price` |
| `ctx.now` | 最新足の時刻 |
| `ctx.params` | `default_params` にコンストラクタ引数をマージしたもの（`self.params` と同じ） |

## Signal

```python
Signal(side, qty=None, reason="", order_type="MKT", limit_price=None)
```

- `side`: `"BUY"` / `"EXIT"` / `"SELL"`（現状は現物ロングのみ。`EXIT`=`SELL`）
- `qty`: `None` なら `params["qty"]`
- `reason`: 通知・履歴・成績画面に出る。指標値を入れておくと後で検証しやすい
- `order_type` / `limit_price`: engine は現状 **成行・終値約定** 固定（指値は P4 で対応）

いまのエンジンは `flat → long → flat` のみ。建玉中の追加 BUY、分割決済、空売りは未対応。
損切り/利確は `on_bar` の中で `ctx.position.avg_price` と現在値を比べて自前で出す。

## 指標ヘルパー（`app/strategy/indicators.py`）

すべて `pd.Series` 入出力。最新値は `.iloc[-1]`、1本前は `.iloc[-2]`。

| 関数 | 説明 |
|---|---|
| `sma(s, n)` / `ema(s, n)` | 単純 / 指数移動平均 |
| `rsi(s, n=14)` | Wilder の RSI（0-100） |
| `atr(high, low, close, n=14)` | Average True Range（損切り幅の目安に） |
| `macd(s, 12, 26, 9)` | `(macd線, signal線, ヒストグラム)` |
| `deviation_pct(price, ma)` | 移動平均からの乖離率（%） |
| `slope_pct(s, lookback)` | `lookback` 本前からの変化率（%）。傾きの判定に |
| `crossed_up(a, b)` / `crossed_down(a, b)` | 最新足で a が b を上抜け / 下抜けしたか（bool） |

## 例

- `examples/sma_cross.py` — 短期/長期 SMA のゴールデン/デッドクロス
- `examples/ma_rsi.py` — 移動平均と終値の関係（上抜け/上方）+ 傾き + RSI 帯でエントリー
- `examples/trend_rsi_reclaim.py` — 短期MA>中期MAでRSIが40を回復→買い / 短期MA<中期MAで
  RSIが60を割れ→売り（ポジションを見ない純粋なアラート戦略。`mode=notify` 向け。
  売り側は現エンジンでは paper/backtest 非対応）

## 調整のしかた

`default_params` を持たせておけば、UI の「戦略」/「バックテスト」画面で
プルダウンを選んだときにパラメータ欄（JSON）へ自動で入る。値を書き換えて
別々の `Strategy` 行として登録すれば、同じロジックの違う設定を並行で回せる。
