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
| `ctx.position` | `.is_flat` / `.is_long` / `.is_short` / `.qty`（売建はマイナス）/ `.avg_price` |
| `ctx.now` | 最新足の時刻 |
| `ctx.params` | `default_params` にコンストラクタ引数をマージしたもの（`self.params` と同じ） |

## Signal

```python
Signal(side, qty=None, reason="", order_type="MKT", limit_price=None)
```

- `side`: `"BUY"`（新規買い）/ `"SHORT"`（新規売建）/ `"EXIT"`（今の建玉を手仕舞い。売建なら
  買戻し）/ `"SELL"`（旧来の書き方。買い建玉の手仕舞いとしてだけ扱う）
- `qty`: `None` なら `params["qty"]`
- `reason`: 通知・履歴・成績画面に出る。指標値を入れておくと後で検証しやすい
- `order_type` / `limit_price`: engine は現状 **成行・終値約定** 固定（指値は P4 で対応）

いまのエンジンは `flat → long → flat` と `flat → short → flat`。同時に持つ建玉は1つで、
建玉中の追加建て・分割決済・ドテン（同じ足で手仕舞い→反対に建てる）はしない。

## 売買方向（direction）と取引区分（trade_type）

`direction`（`"long"`=買いのみ・既定 / `"short"`=売りのみ / `"both"`=両方）・`trade_type`
（`"cash"`=現物 / `"margin"`=信用、既定 `"cash"`）も全戦略共通パラメータ。旧パラメータ
`allow_short`（bool）だけが保存された戦略は true→`both`、それ以外→`long` として読む。

エンジンは `on_bar()` を直接呼ばず `Strategy.decide()` を呼び、方向に合わない新規建て
（`long` なのに `SHORT`、`short` なのに `BUY`）を捨てる（手仕舞いは常に通す）。戦略側は
`self.allow_short` が True のときだけ `SHORT` を返せばよく、`BUY` を方向で出し分ける必要はない。
同梱の8戦略は買いと上下対称な売建ルールを持つ（例: SMA クロスはデッドクロスで売建、ゴールデンクロスで買戻し）。
ショートの損益は (建値 - 手仕舞い値) × 株数、損切り/利確は向きが逆（`stops.py` の `direction`）。
信用の金利・貸株料はバックテスト・ペーパーでは考慮しない。

実発注（mode=live）では空売りは `trade_type="margin"` のときだけ発注する。信用区分は
`hold_overnight=False`（日中足）ならいちにち信用(4)、それ以外は一般信用・無期限(2)
（`live.margin_type_for`）。bridge は信用の新規建て・返済（建玉一覧から建日・建値・建市場を引く）
に対応済み（`bridge/README.md`「信用取引」）。

## 損切り / 利確

`stop_loss_pct` / `take_profit_pct`（建値からの%）は全戦略に共通のパラメータとして
自動で付く（`registry.UNIVERSAL_DEFAULTS` / `UNIVERSAL_META`。戦略クラス側で書く必要はない）。
バックテスト・live 両方で、`on_bar()` の判断より**優先して**その足の高値/安値で判定する
（`app/engine/stops.py`）。同じ足で両方のラインに達したら損切りを優先。空欄（`None`）なら無効
で、今までどおり戦略自身が `ctx.position.avg_price` を見て `on_bar` 内で判断する形も引き続き使える
（両方併用も可。バックテストは `run_backtest` 内でストップ判定が先に走り、当たれば `on_bar` はその
足で呼ばれない）。

## エントリー時間帯（entry_windows）

全戦略共通パラメータ。`"9:00-10:00 13:30-14:30"` のように JST の時間帯を空白区切りで書くと、足が確定した時刻
（＝発注する時刻）がその中のときだけ BUY/SHORT を通す（`Strategy.decide` / `parse_entry_windows`）。
空欄（既定）なら終日。手仕舞いは制限しない。最適化ではカンマが候補の区切りになる。

## 決済条件（exit_rule）

全戦略共通パラメータ。`"signal"`（既定・エントリー条件の反転シグナル）なら `on_bar()` が出す手仕舞い
（EXIT/SELL/COVER）で決済。`"sma_cross"`（SMAクロス）なら建玉があるとき `on_bar()` の手仕舞いは使わず、
買建は短期SMA（`exit_sma_fast`、既定10）が長期SMA（`exit_sma_slow`、既定30）を下抜けた足、売建は
上抜けた足で手仕舞う（`Strategy.decide`）。損切り/利確・大引け手仕舞いはどちらでも効く。

## 大引けをまたぐか（hold_overnight）

`hold_overnight`（既定 `True`＝持ち越す）も全戦略共通パラメータ。`False` にすると日中足
（1m/5m/15m）では、15:20（JST）までに確定する最後の足の終値で手仕舞いし、その足では新規 BUY を
無視する（`app/engine/eod.py`）。判定順は 損切り/利確 → 大引け手仕舞い → `on_bar()`。
バックテストでは次の足が別の日なら、その足も大引けとして扱う。日足では無視。

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
| `bollinger_bands(s, n=20, num_std=2.0)` | `(中心線, 上限, 下限)` = SMA ± num_std×標準偏差 |
| `donchian_upper(high, n)` / `donchian_lower(low, n)` | 現在足を含まない直近 n 本の最高値/最安値（ブレイクアウト判定用） |
| `adx(high, low, close, n=14)` | `(ADX, +DI, -DI)`。ADX が高いほどトレンドが強い（方向は問わない） |

## 例

トレンドフォロー系、逆張り系、フィルタ付きなど性質の違うものを揃えてある。

| 例 | 系統 | 概要 |
|---|---|---|
| `sma_cross.py` | トレンドフォロー | 短期/長期 SMA のゴールデン/デッドクロス |
| `ma_rsi.py` | トレンドフォロー | 終値とMAの関係（上抜け/上方）+ 傾き + RSI帯でエントリー |
| `trend_rsi_reclaim.py` | 押し目・戻り | 短期MA>中期MAでRSIが40回復→買い / 短期MA<中期MAでRSIが60割れ→売り。決済は反対側のシグナル（トレンド反転＋RSI）。任意の絞り込み: `gap_filter`（ギャップの向きにだけ建てる）・`min_pullback`（直近12本の RSI がライン±この値まで行ってからだけ建てる） |
| `macd_cross.py` | モメンタム | MACD線がシグナル線を上抜け/下抜け。`gap_filter`（ギャップの向きにだけ建てる）で絞り込める |
| `bollinger_reversion.py` | **逆張り** | ボリンジャー下限を割れてから反発で買い、中心線/上限で手仕舞い |
| `bollinger_regime.py` | 順張り＋逆張り | バンド幅でスクイーズ/エクスパンションを判定。スクイーズ後の拡大で上限/下限ブレイクに順張り（中心線割れで手仕舞い）、拡大していないときは下限/上限からの戻りに逆張り（中心線で手仕舞い）。`use_trend`/`use_reversion` で片方だけにもできる |
| `donchian_breakout.py` | ブレイクアウト | 直近N本の高値ブレイクで買い、より短いM本の安値割れで手仕舞い（タートル風） |
| `adx_ma_cross.py` | フィルタ付きトレンド | ADXでトレンドの強さを確認してから SMAクロスに従う（レンジ相場のダマシ回避） |
| `gap_breakout.py` | ギャップ・デイトレ | ギャップアップの日に押してから当日高値を終値で更新したら買い（ダウンは逆に売り）。窓埋めで中止、押し安値割れで手仕舞い。2026-09 の検証では売り側が弱く、買いのみ＋10:30 までが有望 |

## パラメータ最適化

`/optimize` でパラメータ範囲を JSON で指定すると（例 `{"fast": [5, 10, 15], "slow": [20, 30, 40]}`）
総当たりでバックテストし、期間を学習/検証に分けて **検証期間（探索に使っていない後半のデータ）の
成績でランキング**する（`app/engine/optimize.py`）。学習期間だけ良い設定＝過学習を避けるため。
検証期間の取引が少ない結果は⚠付きで表示されるので鵜呑みにしないこと。結果から
そのまま `/strategies` にパラメータ入りで登録できる。

## 調整のしかた

`default_params` を持たせておけば、UI の「戦略」/「バックテスト」/「最適化」画面で
プルダウンを選んだときにパラメータ入力欄が自動で組み立てられる（項目ごとの入力欄。
生JSONを書く必要はない）。値を書き換えて別々の `Strategy` 行として登録すれば、
同じロジックの違う設定を並行で回せる。

`param_meta` を足すと、入力欄に日本語ラベル・説明（マウスオーバー）・選択肢（enum的な
文字列パラメータ用のドロップダウン）が付く。省略したキーはそのまま項目名が表示される:

```python
param_meta = {
    "fast": {"label": "短期SMA期間", "help": "短期の単純移動平均を計算する本数"},
    "ma_type": {"label": "移動平均の種類", "choices": ["ema", "sma"]},
}
```

実体は `app/web/static/param_form.js`。バックテスト/戦略登録は単値の入力欄、最適化は
カンマ区切りで複数値（探索範囲）を入れられる（`allowRanges`）。
