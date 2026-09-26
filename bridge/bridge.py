"""RSS ブリッジ本体。

気配を運ぶだけでなく（P4からは）発注のリレーも行う。売買ロジックは持たない
（backend の live エンジンが判断し、ここは Excel(RSS) との橋渡しに徹する）。

2つのモード:
  1) 通常（Windows）: xlwings で rss_bridge.xlsx の quotes シートを読み、backend へ送る
     python bridge.py

  2) シミュレーション（Mac でもOK・Excel不要）: ランダムウォークの気配を生成して送る
     python bridge.py --simulate --codes 7203,6501,9984
     python bridge.py --simulate --set-id 1

共通オプション:
  --once            1回だけ送って終了
  --interval 2.0    ポーリング間隔（秒）。省略時は config の poll_interval_sec
  --ingest-url URL  送信先。省略時は config の ingest_url

発注リレー（P4・未検証）:
  config の orders_workbook_path が設定されていれば、毎ループ backend の
  GET /api/orders/pending を確認し、あれば発注専用ブック（build_workbook.py --orders
  で作成）に RssStockOrder の引数を書き込んでトリガーを立て、セルの表示が
  「発注済み/キャンセル/エラー」等に確定するまで待って POST /api/orders/{id}/report で
  結果を報告する。--simulate 時は発注リレーを行わない。
"""
from __future__ import annotations

import argparse
import random
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

# Windows のコンソール（cp932）でも日本語フィールド名を print できるように
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from app.config import get_config  # noqa: E402

# quotes シートの列並び（build_workbook.py と一致させること）
FIELDS = ["現在値", "出来高", "前日比", "最良買気配値", "最良売気配値"]
FIELD_KEY = {"現在値": "price", "出来高": "volume", "最良買気配値": "bid", "最良売気配値": "ask"}


# ---- 通常モード: Excel から読む ------------------------------------------------


def _open_book(workbook_path: str):
    import xlwings as xw  # Windows のみ

    p = Path(workbook_path)
    for b in xw.books:
        try:
            if Path(b.fullname).resolve() == p.resolve():
                return b
        except Exception:  # noqa: BLE001
            continue
    return xw.Book(workbook_path)


def read_quotes_via_xlwings(workbook_path: str) -> list[dict]:
    book = _open_book(workbook_path)
    ws = book.sheets["quotes"]
    table = ws.range("A2").expand().value or []
    if table and not isinstance(table[0], list):
        table = [table]

    now = datetime.now(UTC).isoformat()
    quotes = []
    for row in table:
        code = row[0]
        if code is None:
            continue
        code = str(int(code)) if isinstance(code, float) else str(code).strip()
        mapping = dict(zip(FIELDS, row[1 : 1 + len(FIELDS)], strict=False))
        q = {"code": code, "ts": now}
        for jp, key in FIELD_KEY.items():
            v = mapping.get(jp)
            q[key] = float(v) if isinstance(v, (int, float)) and v else None
        # 現在値がまだ 0（寄り付き前）でも、気配があれば送る＝ブリッジ生存が分かる。
        # 足は約定（price>0）が出るまで作られない（backend 側で除外）。
        if q.get("price") or q.get("bid") or q.get("ask"):
            quotes.append(q)
    return quotes


def dump_workbook(workbook_path: str) -> None:
    """quotes シートの生の値を型付きで表示する（RSS フィールド名の実地確認用）。"""
    book = _open_book(workbook_path)
    ws = book.sheets["quotes"]
    rng = ws.used_range
    print(f"workbook : {book.fullname}")
    print(f"used_range: {rng.address}")
    vals = rng.value
    if vals and not isinstance(vals[0], list):
        vals = [vals]
    for r, row in enumerate(vals, start=1):
        cells = "  ".join(f"C{c}={v!r}({type(v).__name__})" for c, v in enumerate(row, start=1))
        print(f"  R{r}: {cells}")
    print("\nheader 行(R1)が RSS の項目名。数値が返っていない列は項目名が違う可能性大。")


# ---- 発注リレー（P4・未検証。Windows実機での確認が必要） --------------------

ORDER_ARG_COLS = list("ABCDEFGHIJKLMNOPQRST")  # RssStockOrder の20引数（A〜T列）
ORDER_STATUS_COL = "U"


def _classify_order_status(text: str) -> str:
    """RssStockOrder のセル表示テキストを大まかな状態に分類する。"""
    text = (text or "").strip()
    if not text:
        return "waiting"
    if text.startswith("発注済み"):
        return "sent"
    if text in ("待機中", "接続待ち", "応答待ち"):
        return "waiting"
    if text == "キャンセル":
        return "cancelled"
    # 入力エラー/サーバエラー/発注ロック中/発注ID使用済み 等はまとめて拒否扱い
    return "rejected"


class OrderRelay:
    """発注専用ブックへの書き込み・トリガー・状態確定待ちを行う。

    行は使い捨て（300行を使い切ったら先頭に戻って上書きする。少額試験運用の
    想定なので十分。将来のログ保全が要るなら行数を増やすか別途アーカイブする）。
    """

    def __init__(self, workbook_path: str, n_rows: int = 300):
        self.workbook_path = workbook_path
        self.n_rows = n_rows
        self._next_row = 2

    def _take_row(self) -> int:
        row = self._next_row
        self._next_row += 1
        if self._next_row > self.n_rows + 1:
            self._next_row = 2
        return row

    def place(self, order: dict, resolve_timeout: float) -> dict:
        """1件発注する。{"status": ..., "broker_order_id"?: ..., "error"?: ...} を返す。"""
        book = _open_book(self.workbook_path)
        ws = book.sheets["orders"]
        row = self._take_row()

        side_code = 1 if order["side"] in ("EXIT", "SELL") else 3  # 1:売り 3:買い
        row_values = [
            order["id"], 0, str(order["symbol_code"]), side_code, 0, 0,
            int(order["qty"]), 0, None, 1, None, str(order.get("account_type") or "0"),
            None, None, None, None, None, None, None, None,
        ]
        try:
            ws.range(f"A{row}:T{row}").value = row_values
            ws.range(f"B{row}").value = 1  # 発注トリガー 0->1 で発注実行

            deadline = time.time() + resolve_timeout
            text = ""
            kind = "waiting"
            while time.time() < deadline:
                text = ws.range(f"{ORDER_STATUS_COL}{row}").value or ""
                kind = _classify_order_status(text)
                if kind != "waiting":
                    break
                time.sleep(0.5)

            if kind == "waiting":
                return {"status": "timeout", "error": f"未確定のまま待機時間切れ: {text!r}"}
            if kind == "sent":
                return {"status": "sent", "broker_order_id": text}
            if kind == "cancelled":
                return {"status": "cancelled", "error": text}
            return {"status": "rejected", "error": text}
        finally:
            try:
                ws.range(f"B{row}").value = 0  # 次にこの行を使うときのため戻しておく
            except Exception:  # noqa: BLE001
                pass


def run_order_relay(orders_url: str, relay: OrderRelay, resolve_timeout: float, client: httpx.Client) -> int:
    """backend の未処理注文を1回ぶん処理する。処理件数を返す。"""
    r = client.get(f"{orders_url}/pending")
    r.raise_for_status()
    pending = r.json().get("orders", [])
    for order in pending:
        try:
            result = relay.place(order, resolve_timeout)
        except Exception as e:  # noqa: BLE001
            result = {"status": "error", "error": str(e)}
        client.post(f"{orders_url}/{order['id']}/report", json=result)
        print(f"{datetime.now():%H:%M:%S} order#{order['id']} {order['symbol_code']} "
              f"{order['side']} x{order['qty']} -> {result}")
    return len(pending)


# ---- シミュレーションモード -------------------------------------------------


class Simulator:
    def __init__(self, codes: list[str]):
        self.codes = codes
        rnd = random.Random(42)
        self.price = {c: rnd.uniform(800, 4000) for c in codes}
        self.cum_vol = {c: 0.0 for c in codes}

    def poll(self) -> list[dict]:
        now = datetime.now(UTC).isoformat()
        out = []
        for c in self.codes:
            p = max(1.0, self.price[c] * (1 + random.uniform(-0.0015, 0.0015)))
            self.price[c] = p
            self.cum_vol[c] += random.randint(0, 500) * 100
            spread = max(0.1, round(p * 0.0005, 1))
            out.append(
                {
                    "code": c,
                    "ts": now,
                    "price": round(p, 1),
                    "volume": self.cum_vol[c],
                    "bid": round(p - spread, 1),
                    "ask": round(p + spread, 1),
                }
            )
        return out


def _codes_from_set(set_id: int) -> list[str]:
    from sqlmodel import Session, select

    from app.db import engine, init_db
    from app.models import SymbolSetItem

    init_db()
    with Session(engine) as s:
        items = s.exec(
            select(SymbolSetItem).where(SymbolSetItem.set_id == set_id).order_by(SymbolSetItem.sort_order)
        ).all()
    return [it.symbol_code for it in items]


# ---- メインループ ----------------------------------------------------------


def main() -> None:
    cfg = get_config().bridge
    ap = argparse.ArgumentParser()
    ap.add_argument("--simulate", action="store_true")
    ap.add_argument("--dump", action="store_true", help="Excel の生の値を表示して終了（RSS 項目名の確認用）")
    ap.add_argument("--codes", help="カンマ区切り 例: 7203,6501")
    ap.add_argument("--set-id", type=int)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--interval", type=float, default=cfg.poll_interval_sec)
    ap.add_argument("--ingest-url", default=cfg.ingest_url)
    ap.add_argument("--workbook", default=cfg.workbook_path, help="rss_bridge.xlsx のパス")
    ap.add_argument(
        "--orders-workbook", default=cfg.orders_workbook_path,
        help="発注専用ブック（build_workbook.py --orders で生成）。指定時のみ発注リレーを行う",
    )
    ap.add_argument("--orders-url", default=cfg.orders_url)
    ap.add_argument("--order-timeout", type=float, default=cfg.order_resolve_timeout_sec)
    args = ap.parse_args()

    if args.dump:
        dump_workbook(args.workbook)
        return

    order_relay = None
    if args.orders_workbook and not args.simulate:
        order_relay = OrderRelay(args.orders_workbook)
        print(f"order relay: {args.orders_workbook} <-> {args.orders_url}")

    if args.simulate:
        codes = (
            args.codes.split(",")
            if args.codes
            else _codes_from_set(args.set_id)
            if args.set_id
            else []
        )
        codes = [c.strip() for c in codes if c.strip()]
        if not codes:
            raise SystemExit("--simulate には --codes か --set-id が必要です")
        sim = Simulator(codes)
        source = lambda: sim.poll()  # noqa: E731
        print(f"bridge (simulate): {codes} -> {args.ingest_url} ({args.interval}s)")
    else:
        source = lambda: read_quotes_via_xlwings(args.workbook)  # noqa: E731
        print(f"bridge: {args.workbook} -> {args.ingest_url} ({args.interval}s)")

    client = httpx.Client(timeout=10)
    while True:
        try:
            quotes = source()
            if quotes:
                r = client.post(args.ingest_url, json={"quotes": quotes})
                r.raise_for_status()
                print(f"{datetime.now():%H:%M:%S} sent {len(quotes)} quotes -> {r.json()}")
        except Exception as e:  # noqa: BLE001
            print(f"[warn] {e}")

        if order_relay is not None:
            try:
                run_order_relay(args.orders_url, order_relay, args.order_timeout, client)
            except Exception as e:  # noqa: BLE001
                print(f"[warn][orders] {e}")

        if args.once:
            break
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
