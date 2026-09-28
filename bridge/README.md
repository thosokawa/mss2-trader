# bridge/ — RSS ブリッジ

「価格を運ぶだけ」の薄い層。売買ロジックは一切持たない（backend の live エンジンが判断する）。

1. `build_workbook.py` … 銘柄セット（または `--codes`）から RSS 関数を敷いた `rss_bridge.xlsx` を生成
2. `bridge.py` … MarketSpeed II ログイン後にそのブックを開き、xlwings でセルを
   ポーリング読み取り → backend の `/api/ingest` へ POST
   - `--simulate` … Excel 不要のシミュレーションモード（Mac で疎通確認できる）
3. （P4・ステージ1確認済み）発注リレー: `build_workbook.py --orders` で発注専用ブックを1回作り、
   `bridge.py --orders-workbook <path>` で backend の発注キューを拾って
   `RssStockOrder` に書き込み・結果を報告する（下記「実発注（P4）」参照）

`bridge.py` の `FIELDS` と `build_workbook.py` の `FIELDS` は必ず一致させること。

## Windows 実機セットアップ

```
py -3.13 -m venv .venv
.venv\Scripts\pip install -r ..\requirements-bridge.txt
```

## 手順（Windows）

1. マーケットスピードII を起動しログイン、「RSS」を有効化
2. `python build_workbook.py --set-id 1` で `rss_bridge.xlsx` を生成
3. 生成された `rss_bridge.xlsx` を Excel で開く（RSS 関数が値を返し始める）
4. `python bridge.py` を起動 → backend にティックが流れる

## Mac での疎通確認（Excel 不要）

```
python bridge.py --simulate --codes 7203,6501,9984
python bridge.py --simulate --set-id 1
```

## RSS 項目名（実機確認済み 2026-09）

`=RssMarket(code, "項目名")` で以下が有効（`build_workbook.py --probe <code>` で確認できる）:

- 使用中: `現在値` `出来高` `前日比` `最良買気配値` `最良売気配値`
- 他に有効: `銘柄名称` `現在値時刻` `売買代金` `始値` `高値` `安値` `前日終値`
  `前日比率` `最良売気配数量` `最良買気配数量` `市場コード`
- 無効だったもの: `売気配値1` `買気配値1` `VWAP` `約定回数`

`.xlsx` のままで RSS 関数は自動更新される（`.xlsm` は VBA を足すとき）。

## 未確定事項（楽天証券の公式 RSS リファレンスで要確認）

- リアルタイム登録銘柄数の上限

## 実発注（P4・ステージ1確認済み / 実弾は未検証）

`RssStockOrder`（国内株式・現物注文）の引数・戻り値・注文一覧系関数は公式リファレンス
（PDF）で確認済み。試す前に必ず以下を確認すること。

### 実機検証の記録

- **2026-09-28 ステージ1 OK**（下記「安全な確認手順」の 1〜3）: MarketSpeed II の発注機能
  OFF のまま、9984 BUY 100株（MACD 1分足、mode=live、ARMED）のシグナルから Order が作られ、
  bridge が `rss_orders.xlsx` に書き込み、`RssStockOrder` が
  「発注ロック中(発注を行うには発注機能を有効にしてください)」を返して約1.3秒で
  `status="rejected"` として報告された。
  - この過程で見つかったバグ（修正済み）: RiskEngine の取引時間判定が UTC を JST として
    比べていた（全シグナルが「取引時間外」）。
  - 100株単位だと値がさ株は `max_notional_per_order`（既定30万円）に掛かって見送られる
    （「金額NG」）。実弾の最初は低位株で試すのが安全。
- **ステージ2（発注機能 ON・実弾）: 未実施。** 確認画面の有無（出ると `cancelled` になる）、
  受理後の約定は MarketSpeed II の注文照会で手動確認すること。

### セットアップ

```powershell
# 1回だけ発注専用ブックを作る（quotesブックと違い自動では作り直さない）
python bridge\build_workbook.py --orders          # bridge\rss_orders.xlsx を生成
# Excel で開いたままにする（run_all.ps1 は存在すれば自動で開く）

# bridge 起動時に発注リレーを有効化
python bridge\bridge.py --workbook rss_bridge.xlsx --orders-workbook rss_orders.xlsx
```

`config.toml` の `[bridge] orders_workbook_path` を設定しておけば `run_all.ps1` が
自動的に発注専用ブックを開き、`--orders-workbook` を付けて bridge を起動する。

### 安全な確認手順（推奨）

1. **MarketSpeed II の「発注機能」をまず OFF のまま**にする
2. `/risk` で ARM し、`/strategies` で1つだけ `mode=live` の戦略を有効化（少額のテスト銘柄で）
3. シグナルが出たら Order が作られ、bridge がセルに書き込む → `RssStockOrder` のセルが
   「発注ロック中（発注を行うには発注機能を有効にしてください）」を返すはず
   → backend 側で `status="rejected"` として正しく検出できるか確認する
4. ここまで確認できて初めて MarketSpeed II の発注機能を ON にする（少額・小口数で）
5. 注文確認画面が出る設定だと `status="cancelled"`（ダイアログを閉じただけ）になる。
   自動化するなら確認画面を出さない設定が必要（MarketSpeed II 側の設定を確認）

### 信用取引（実装中）

公式オンラインヘルプで確認した仕様:
- 新規 `RssMarginOpenOrder(発注ID,発注トリガー,銘柄コード,売買区分,注文区分,SOR区分,信用区分,
  注文数量,価格区分,注文価格,執行条件,注文期限,口座区分,逆指値…,セット注文…)`（22引数）。
  売買区分 1=売建 3=買建、信用区分 1=制度 2=一般(無期限) 3=一般(14日) 4=一般(いちにち)、口座区分 0/1
- 返済 `RssMarginCloseOrder(…,信用区分,注文数量,価格区分,注文価格,執行条件,注文期限,口座区分,
  建日,建単価,建市場,逆指値…)`（20引数）。売買区分 1=売埋 3=買埋。**建日(YYYYMMDD)・建単価・
  建市場(1=東証…)は必須で省略不可** → 返済する建玉を `RssMarginPositionList` で特定する必要がある
- `RssMarginPositionList` の取得項目: 銘柄コード, 銘柄名称, 口座区分, 建市場, 信用区分, 弁済期限,
  売買, 建玉数量, 発注数量, 建値, 建日, 最終返済日, 時価, …
- 値の形式（建日が日付型か文字列か、建市場が「東証」か等）は未確認。信用建玉がある状態で:
  ```powershell
  python bridge\build_workbook.py --probe-account      # bridge\rss_account_probe.xlsx を生成して Excel で開く
  python bridge\bridge.py --dump --workbook bridge\rss_account_probe.xlsx --sheet positions
  ```

それまで backend は信用の新規建てを発注しない（`live.MARGIN_ORDERS_SUPPORTED=False`）。bridge も
`trade_type=margin` や SHORT/COVER の注文は Excel に書かずに `rejected` を返す（二重の防御）。

### 既知の制限（v1）

- 成行注文のみ（指値・逆指値・信用取引・セット注文は未対応。RssStockOrder の引数は
  すべて用意してあるので拡張は可能）
- 「発注済み」（受理）までしか自動確認しない。**実際に約定したかは `RssOrderStatus` /
  `RssOrderList` を見て手動で確認する**（自動フォローアップは未実装）
- 受理（`sent`）された成行注文は **シグナル時点の価格（`Order.ref_price`）で約定したとみなして**
  建玉を進める（`app/engine/orders.py`）。建値もこの価格なので、損切り/利確の判定・日次損失
  リミットに足す実現損益は実際の約定価格とずれうる（概算）。売り（戦略の EXIT・損切り/利確・
  大引け手仕舞い）は受理後に自動で出る。建玉がある間の BUY、建玉が無いときの EXIT は発注しない。
- bridge の報告が `timeout` / `error`（発注されたか不明）のときは **自動で DISARM** する。
  MarketSpeed II の注文照会で実際の状態を確認し、必要なら手動で売買してから ARM し直す
  （DB 上は「発注されなかった」扱いなので、実は約定していた場合は建玉の認識がずれる）。
- 日次の実現損益はプロセス内メモリで集計する（ARMED と同じく backend 再起動でリセット）。
- 発注専用ブックの行は発注ID（= Order.id）から決める（`2 + (id-1) % 300`）。bridge を
  再起動しても前の注文の行を使い回さない。300件ごとに一周して上書きする
- ステータス列は実機では「`=@RssStockOrder(A2,...) => 発注ロック中(...)`」のように数式が前に
  付いて返る。`=>` の後ろだけで判定する。「発注済み(発注ID=xxxx)」は ID が一致したときだけ
  受理とみなし、拒否系の表示は 1.5 秒続いてから確定する（古い表示の読み違い対策）。
  「発注ID使用済み」は既に発注済みの可能性があるので `error`（→ backend が自動 DISARM）
