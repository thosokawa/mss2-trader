"""RSS ブリッジ本体。

気配を運ぶだけでなく（P4からは）発注のリレーも行う。売買ロジックは持たない
（backend の live エンジンが判断し、ここは Excel(RSS) との橋渡しに徹する）。

2つのモード:
  1) 通常（Windows）: xlwings で rss_bridge.xlsx の quotes シートを読み、backend へ送る
     python bridge.py

  2) シミュレーション（Mac でもOK・Excel不要）: ランダムウォークの気配を生成して送る
     python bridge.py --simulate --codes 7203,6501,9984
     python bridge.py --simulate          # 取り込む銘柄は backend の /api/quote-codes から

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
import re
import sys
import time
from datetime import UTC, datetime, timedelta
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

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rss_layout import (  # noqa: E402
    ORDER_SHEETS,
    POSITION_ITEMS,
    POSITIONS_HEADER_ROW,
    POSITIONS_SHEET,
    col_letter,
    order_formula,
    positions_formula,
    status_col,
)

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


def dump_workbook(workbook_path: str, sheet: str = "quotes") -> None:
    """シートの生の値を型付きで表示する（RSS 項目名・値の形式の実地確認用）。"""
    book = _open_book(workbook_path)
    ws = book.sheets[sheet]
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

# 取り込む銘柄を backend に問い合わせて quotes シートを合わせる間隔（秒）
QUOTE_SYNC_SEC = 30

ORDER_STATUS_COL = status_col("orders")  # RssStockOrder のステータス列（U）

# 信用返済は建玉1つ（建日・建単価・建市場の組）ごとに1注文になり、1つの Order が複数の
# RssMarginCloseOrder に分かれることがある。その発注IDは Order.id と重ならない別の範囲から振る。
CLOSE_ID_BASE = 1_000_000_000
CLOSE_ID_SLOTS = 10  # 1 Order あたりの返済注文の上限

MARKET_CODE = {"東証": 1, "名証": 3, "JNX": 4, "JAX": 5}
ACCOUNT_CODE = {"特定": "0", "一般": "1"}
MARGIN_TYPE_BY_TERM = {"1日": 4, "無期限": 2, "14日": 3}


def _num(v) -> float | None:
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


def parse_positions(table: list[list]) -> list[dict]:
    """margin_positions シート（1行目が見出し）の値から建玉のリストを作る。

    実機の値（2026-09-28 確認）: 銘柄コード=5401.0, 口座区分='特定', 建市場='東証'/'JAX',
    信用区分='一般'/'制度', 弁済期限='1日'/'無期限', 売買='買建'/'売建', 建玉数量=100.0,
    発注数量=0.0（返済注文中の数量）, 建値=688.4, 建日=20260928.0。末尾に '--------' の行。
    """
    if not table:
        return []
    header = [str(h).strip() if h is not None else "" for h in table[0]]
    idx = {name: i for i, name in enumerate(header)}

    lots = []
    for row in table[1:]:
        if not row:
            break

        def get(name, row=row):
            i = idx.get(name)
            return row[i] if i is not None and i < len(row) else None

        code_n = _num(get("銘柄コード"))
        if code_n is None:
            break  # '--------'（一覧の終わり）や「応答待ち」
        term = str(get("弁済期限") or "").strip()
        kind = str(get("信用区分") or "").strip()
        margin_type = 1 if kind == "制度" else MARGIN_TYPE_BY_TERM.get(term)
        date_n = _num(get("建日"))
        lots.append({
            "code": str(int(code_n)),
            "account": ACCOUNT_CODE.get(str(get("口座区分") or "").strip()),
            "market": MARKET_CODE.get(str(get("建市場") or "").strip()),
            "margin_type": margin_type,
            "side": str(get("売買") or "").strip(),
            "qty": int(_num(get("建玉数量")) or 0),
            "ordered": int(_num(get("発注数量")) or 0),
            "price": _num(get("建値")),
            "date": int(date_n) if date_n else None,
        })
    return lots


def today_yyyymmdd() -> int:
    """JST の今日（建日と同じ yyyymmdd の整数）。"""
    d = (datetime.now(UTC) + timedelta(hours=9)).date()
    return d.year * 10000 + d.month * 100 + d.day


def fill_from_diff(
    before: list[dict], after: list[dict], order: dict, today: int
) -> tuple[int, float | None]:
    """新規建ての前後の建玉一覧を比べ、この注文で増えた建玉の株数と加重平均の建単価を返す。

    同じ銘柄・売買・信用区分・口座区分で建日が今日の建玉の、(建値, 建市場) ごとの数量の増分を足す。
    約定が複数の値段・市場に分かれても合算する。増えていなければ (0, None)。
    """
    want_side = "売建" if order["side"] == "SHORT" else "買建"

    def keyed(lots):
        out: dict[tuple, int] = {}
        for lot in lots:
            if (lot["code"] == str(order["symbol_code"]) and lot["side"] == want_side
                    and lot["margin_type"] == int(order.get("margin_type") or 0)
                    and lot["account"] == str(order.get("account_type") or "0")
                    and lot["date"] == today and lot["price"]):
                k = (lot["price"], lot["market"])
                out[k] = out.get(k, 0) + lot["qty"]
        return out

    b, a = keyed(before), keyed(after)
    qty, amount = 0, 0.0
    for (price, _market), q in a.items():
        dq = q - b.get((price, _market), 0)
        if dq > 0:
            qty += dq
            amount += price * dq
    if not qty:
        return 0, None
    return qty, round(amount / qty, 4)


def select_lots(lots: list[dict], order: dict) -> tuple[list[tuple[dict, int]], str]:
    """返済注文に使う建玉と数量を選ぶ。([(建玉, 数量), ...], 見つからない理由) を返す。

    bot が建てた建玉だけを対象にするため、銘柄・売買・信用区分・口座区分に加えて
    建日が bot の新規建ての日（order["open_date"]）と一致するものに限る。
    （同じ日に同じ銘柄・同じ信用区分で手動でも建てていると区別できない — README 参照）
    """
    want_side = "売建" if order["side"] == "COVER" else "買建"
    need = int(order["qty"])
    open_date = int(order.get("open_date") or 0)
    cands = [
        lot for lot in lots
        if lot["code"] == str(order["symbol_code"])
        and lot["side"] == want_side
        and lot["margin_type"] == int(order.get("margin_type") or 0)
        and lot["account"] == str(order.get("account_type") or "0")
        and (not open_date or lot["date"] == open_date)
        and lot["market"] is not None and lot["price"]
        and lot["qty"] - lot["ordered"] > 0
    ]
    cands.sort(key=lambda lot: lot["qty"] - lot["ordered"], reverse=True)
    picked, left = [], need
    for lot in cands:
        if left <= 0:
            break
        q = min(left, lot["qty"] - lot["ordered"])
        picked.append((lot, q))
        left -= q
    if left > 0:
        have = need - left
        return [], (f"返済できる建玉が足りない（必要{need}株 / 該当{have}株: {order['symbol_code']} "
                    f"{want_side} 信用区分{order.get('margin_type')} 建日{open_date or '指定なし'}）")
    if len(picked) > CLOSE_ID_SLOTS:
        return [], f"建玉が{len(picked)}件に分かれていて返済注文の上限（{CLOSE_ID_SLOTS}件）を超える"
    return picked, ""


def _status_text(value: object) -> str:
    """ステータス列のセル値を判定用の文字列にする。

    実機では「=@RssStockOrder(A2,...,T2) => 発注ロック中(...)」のように数式が前に付いて
    返ってきたので、"=>" があればその後ろだけを使う。
    """
    text = "" if value is None else str(value)
    if "=>" in text:
        text = text.rsplit("=>", 1)[1]
    return text.strip()


# 実機（2026-09-29）の受理表示は「発注済み(注文ID=15)」。リファレンスの表記「発注ID」も受け付ける
_ORDER_ID_RE = re.compile(r"(?:注文|発注)ID\s*[=＝:：]\s*(\d+)")


def _classify_order_status(text: str, order_id: int | None = None) -> str:
    """RssStockOrder のセル表示テキストを大まかな状態に分類する。

    order_id を渡すと「発注済み(注文ID=xxxx)」の ID が一致するときだけ sent とする
    （一致しなければ前の注文の表示が残っているとみなして waiting）。
    """
    text = _status_text(text)
    if not text:
        return "waiting"
    if text.startswith("発注済み"):
        m = _ORDER_ID_RE.search(text)
        if order_id is not None and m and int(m.group(1)) != int(order_id):
            return "waiting"
        return "sent"
    if text in ("待機中", "接続待ち", "応答待ち"):
        return "waiting"
    if text == "キャンセル":
        return "cancelled"
    if "使用済み" in text:
        # 同じ発注IDで既に発注されている＝実際に注文が出ているかもしれない。
        # backend に error として報告し、自動 DISARM → 人が注文照会で確認する。
        return "error"
    # 入力エラー/サーバエラー/発注ロック中 等はまとめて拒否扱い
    return "rejected"


class OrderRelay:
    """発注専用ブックへの書き込み・トリガー・状態確定待ちを行う。

    書き込む行は発注ID（= backend の Order.id）から決める: 2 + (id-1) % n_rows。
    bridge を再起動しても前の注文の行を使い回さない（使い回すと、トリガー直後に前の
    注文の結果表示を読んでしまう恐れがある）。n_rows 件ごとに一周して上書きする。
    """

    # 拒否系の表示は、この秒数だけ同じ表示が続いてから確定する（トリガー直前の古い
    # 表示を読んでしまい、実は発注されている注文を「拒否」と誤判定しないため）
    REJECT_SETTLE_SEC = 1.5
    # 信用の新規建てが受理されたあと、建玉一覧に約定が現れるのを待つ回数と間隔（秒）
    FILL_CHECK_TRIES = 3
    FILL_CHECK_WAIT_SEC = 1.5

    def __init__(self, workbook_path: str, n_rows: int = 300, sor: int = 1):
        self.workbook_path = workbook_path
        self.n_rows = n_rows
        self.sor = 1 if int(sor) else 0  # SOR区分（config の bridge.sor。手数料ゼロコースは 1 必須）

    def row_for(self, order_id: int) -> int:
        return 2 + (int(order_id) - 1) % self.n_rows

    def place(self, order: dict, resolve_timeout: float) -> dict:
        """1件発注する。{"status": ..., "broker_order_id"?: ..., "error"?: ...} を返す。"""
        trade_type = order.get("trade_type") or "cash"
        side = order["side"]
        if trade_type == "cash" and side in ("BUY", "EXIT", "SELL"):
            return self._place_stock(order, resolve_timeout)
        if trade_type == "margin" and side in ("BUY", "SHORT"):
            return self._place_margin_open(order, resolve_timeout)
        if trade_type == "margin" and side in ("EXIT", "SELL", "COVER"):
            return self._place_margin_close(order, resolve_timeout)
        # 想定外の組み合わせ。Excel に一切書かずに拒否する＝確実に未発注。
        return {"status": "rejected", "error": f"未対応の注文: trade_type={trade_type} side={side}"}

    # ---- シート準備 ----

    def _book(self):
        book = _open_book(self.workbook_path)
        self.ensure_sheets(book)
        return book

    def ensure_sheets(self, book) -> None:
        """信用のシート（rss_layout）が無い古いブックなら追加する（作り直し不要にするため）。"""
        names = {s.name for s in book.sheets}
        for sheet, (_func, args) in ORDER_SHEETS.items():
            if sheet in names:
                continue
            ws = book.sheets.add(sheet, after=book.sheets[-1])
            ws.range("A1").value = [*args, "ステータス"]
            col = status_col(sheet)
            ws.range(f"{col}2:{col}{self.n_rows + 1}").formula = [
                [order_formula(sheet, r)] for r in range(2, self.n_rows + 2)
            ]
            print(f"[orders] シート {sheet} を追加しました")
        if POSITIONS_SHEET not in names:
            ws = book.sheets.add(POSITIONS_SHEET, after=book.sheets[-1])
            self._layout_positions(ws)
            print(f"[orders] シート {POSITIONS_SHEET} を追加しました")
        else:
            ws = book.sheets[POSITIONS_SHEET]
            if not str(ws.range("A1").formula or "").startswith("=RssMarginPositionList"):
                # 古い配置（1行目に項目名・A2 に数式 → 結果に上書きされて一覧が固定）を作り直す
                self._layout_positions(ws)
                print(f"[orders] シート {POSITIONS_SHEET} を正しい配置に作り直しました")

    @staticmethod
    def _layout_positions(ws) -> None:
        ws.clear_contents()
        ws.range("A2").value = POSITION_ITEMS
        ws.range("A1").formula = positions_formula()

    # ---- 発注 ----

    def _fire(self, ws, sheet: str, order_id: int, values: list, resolve_timeout: float) -> dict:
        """1行に引数を書いてトリガーを立て、ステータスが確定するまで待つ。"""
        row = self.row_for(order_id)
        last = col_letter(len(ORDER_SHEETS[sheet][1]))
        col = status_col(sheet)
        try:
            ws.range(f"A{row}:{last}{row}").value = values
            ws.range(f"B{row}").value = 1  # 発注トリガー 0->1 で発注実行
            return self._wait_result(lambda: ws.range(f"{col}{row}").value, order_id, resolve_timeout)
        finally:
            try:
                ws.range(f"B{row}").value = 0  # 次にこの行を使うときのため戻しておく
            except Exception:  # noqa: BLE001
                pass

    def _place_stock(self, order: dict, resolve_timeout: float) -> dict:
        side_code = 1 if order["side"] in ("EXIT", "SELL") else 3  # 1:売り 3:買い
        values = [
            order["id"], 0, str(order["symbol_code"]), side_code, 0, self.sor,
            int(order["qty"]), 0, None, 1, None, str(order.get("account_type") or "0"),
            None, None, None, None, None, None, None, None,
        ]
        ws = self._book().sheets["orders"]
        return self._fire(ws, "orders", order["id"], values, resolve_timeout)

    def _place_margin_open(self, order: dict, resolve_timeout: float) -> dict:
        margin_type = int(order.get("margin_type") or 0)
        if margin_type not in (1, 2, 3, 4):
            return {"status": "rejected", "error": f"信用区分が不正: {margin_type}"}
        side_code = 1 if order["side"] == "SHORT" else 3  # 1:売建 3:買建
        values = [
            order["id"], 0, str(order["symbol_code"]), side_code, 0, self.sor, margin_type,
            int(order["qty"]), 0, None, 1, None, str(order.get("account_type") or "0"),
            None, None, None, None,
            None, None, None, None, None,
        ]
        book = self._book()
        before = self.fresh_positions(book) or []
        res = self._fire(book.sheets["margin_open"], "margin_open", order["id"], values, resolve_timeout)
        if res.get("status") == "sent":
            # 受理のあと建玉一覧に約定が現れたら、増えた建玉から実際の建単価・株数を求める
            for _ in range(self.FILL_CHECK_TRIES):
                time.sleep(self.FILL_CHECK_WAIT_SEC)
                after = self.fresh_positions(book)
                filled_qty, avg = fill_from_diff(before, after or [], order, today_yyyymmdd())
                if filled_qty:
                    res.update(filled_qty=filled_qty, avg_price=avg)
                    break
        return res

    def refresh_positions(self, book, timeout: float = 15.0, clock=time.time, sleep=time.sleep) -> bool:
        """建玉一覧を取り直す。RSS の一覧関数は一度取ったデータを使い回すので、A1 の数式を
        消して入れ直すと最新を取りに行く（2026-09-29 実機で確認）。状態が「配信中」か「完了」に
        なれば True、時間内にならなければ False。"""
        ws = book.sheets[POSITIONS_SHEET]
        ws.range("A1").clear_contents()
        sleep(1.0)
        ws.range("A1").formula = positions_formula()
        deadline = clock() + timeout
        while clock() < deadline:
            status = str(ws.range("A1").value or "")
            if "配信中" in status or "完了" in status:
                sleep(0.5)  # 行の書き込みが終わるのを少し待つ
                return True
            sleep(0.1)
        return False

    def read_positions(self, book) -> list[dict]:
        table = book.sheets[POSITIONS_SHEET].used_range.value or []
        if table and not isinstance(table[0], list):
            table = [table]
        # 1行目は数式（状態表示）。2行目が項目名、3行目からデータ
        return parse_positions(table[POSITIONS_HEADER_ROW - 1:])

    def fresh_positions(self, book) -> list[dict] | None:
        """取り直してから読む。取り直せなければ None。"""
        return self.read_positions(book) if self.refresh_positions(book) else None

    def _place_margin_close(self, order: dict, resolve_timeout: float) -> dict:
        book = self._book()
        lots = self.fresh_positions(book)
        if lots is None:
            return {"status": "error",
                    "error": "建玉一覧を取り直せなかった（RssMarginPositionList が応答しない）"}
        picked, why = select_lots(lots, order)
        if not picked:
            # 建玉の認識がずれている（手動で返済済み等）か一覧が未取得。error → backend が DISARM。
            return {"status": "error", "error": why}
        side_code = 3 if order["side"] == "COVER" else 1  # 1:売埋 3:買埋
        ws = book.sheets["margin_close"]
        results = []
        for k, (lot, qty) in enumerate(picked):
            sub_id = CLOSE_ID_BASE + int(order["id"]) * CLOSE_ID_SLOTS + k
            values = [
                sub_id, 0, str(order["symbol_code"]), side_code, 0, self.sor, lot["margin_type"],
                qty, 0, None, 1, None, str(order.get("account_type") or "0"),
                lot["date"], lot["price"], lot["market"],
                None, None, None, None,
            ]
            res = self._fire(ws, "margin_close", sub_id, values, resolve_timeout)
            results.append(res)
            if res["status"] != "sent":
                break
        return combine_close_results(results, len(picked))

    def _wait_result(self, read_cell, order_id: int, resolve_timeout: float,
                     poll_sec: float = 0.5, clock=time.time, sleep=time.sleep) -> dict:
        """ステータス列を読み続け、確定した結果を返す（Excel に触れる部分は read_cell のみ）。"""
        deadline = clock() + resolve_timeout
        text = ""
        reject_text, reject_since = None, 0.0
        while clock() < deadline:
            raw = read_cell()
            text = _status_text(raw)
            kind = _classify_order_status(text, order_id)
            if kind == "sent":
                return {"status": "sent", "broker_order_id": text}
            if kind == "cancelled":
                return {"status": "cancelled", "error": text}
            if kind == "error":
                return {"status": "error", "error": f"{text}（同じ発注IDで既に発注済みの可能性）"}
            if kind == "rejected":
                if text != reject_text:
                    reject_text, reject_since = text, clock()
                elif clock() - reject_since >= self.REJECT_SETTLE_SEC:
                    return {"status": "rejected", "error": str(raw)}
            else:
                reject_text = None
            sleep(poll_sec)
        if reject_text is not None:
            return {"status": "rejected", "error": text}
        return {"status": "timeout", "error": f"未確定のまま待機時間切れ: {text!r}"}


def combine_close_results(results: list[dict], n_planned: int) -> dict:
    """建玉ごとの返済注文の結果を1つの Order の結果にまとめる。"""
    sent = [r for r in results if r["status"] == "sent"]
    if len(sent) == n_planned:
        return {"status": "sent", "broker_order_id": " / ".join(r["broker_order_id"] for r in sent)}
    last = results[-1]
    if not sent:
        return last  # 1件も出ていない（rejected ならそのまま＝確実に未発注）
    # 一部だけ返済注文が出た → 建玉の認識がずれるので error（backend が DISARM）
    return {"status": "error",
            "error": f"返済注文が一部だけ発注された（{len(sent)}/{n_planned}件）: {last.get('error', '')}"}


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


def fetch_quote_codes(client: httpx.Client, url: str) -> list[str]:
    """backend から株価を取り込む銘柄（有効な戦略の対象銘柄 ∪ 監視銘柄）を取る。"""
    r = client.get(url)
    r.raise_for_status()
    return [str(c) for c in r.json().get("codes", [])]


def _code_str(v) -> str:
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip() if v is not None else ""


def sync_quote_sheet(workbook_path: str, codes: list[str]) -> bool:
    """rss_bridge.xlsx の quotes シートの銘柄を codes に合わせる（違うときだけ書き換える）。

    ブックを開き直すと RSS の「発注可能」が解除されるので、開いているブックのセルだけを
    書き換える（A列にコード、B列以降に =RssMarket(A行,"項目")）。書き換えたら True。
    """
    book = _open_book(workbook_path)
    ws = book.sheets["quotes"]
    current = ws.range("A2").expand("down").value if ws.range("A2").value is not None else []
    if not isinstance(current, list):
        current = [current]
    if [_code_str(v) for v in current] == list(codes):
        return False
    last_col = chr(ord("A") + len(FIELDS))
    n_old = max(len(current), 1)
    ws.range(f"A2:{last_col}{n_old + 1}").clear_contents()
    if codes:
        # 文字列として入れる（'130A' 等の英字入りコードもそのまま、数値化で先頭0が消えないように）
        ws.range(f"A2:A{len(codes) + 1}").number_format = "@"
        ws.range("A2").options(transpose=True).value = [str(c) for c in codes]
        ws.range(f"B2:{last_col}{len(codes) + 1}").formula = [
            [f'=RssMarket(A{i},"{f}")' for f in FIELDS] for i in range(2, len(codes) + 2)
        ]
    return True


def main() -> None:
    cfg = get_config().bridge
    ap = argparse.ArgumentParser()
    ap.add_argument("--simulate", action="store_true")
    ap.add_argument("--dump", action="store_true", help="Excel の生の値を表示して終了（RSS 項目名の確認用）")
    ap.add_argument("--sheet", default="quotes", help="--dump で表示するシート名")
    ap.add_argument("--codes", help="カンマ区切り 例: 7203,6501")
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
        dump_workbook(args.workbook, args.sheet)
        return

    order_relay = None
    if args.orders_workbook and not args.simulate:
        order_relay = OrderRelay(args.orders_workbook, sor=cfg.sor)
        print(f"order relay: {args.orders_workbook} <-> {args.orders_url}")

    client = httpx.Client(timeout=10)
    quote_codes_url = args.ingest_url.rsplit("/api/", 1)[0] + "/api/quote-codes"

    if args.simulate:
        codes = [c.strip() for c in (args.codes or "").split(",") if c.strip()]
        if not codes:
            codes = fetch_quote_codes(client, quote_codes_url)
        if not codes:
            raise SystemExit("--simulate: 取り込む銘柄がありません（--codes か、戦略/監視銘柄を登録）")
        sim = Simulator(codes)
        source = lambda: sim.poll()  # noqa: E731
        print(f"bridge (simulate): {codes} -> {args.ingest_url} ({args.interval}s)")
    else:
        source = lambda: read_quotes_via_xlwings(args.workbook)  # noqa: E731
        print(f"bridge: {args.workbook} -> {args.ingest_url} ({args.interval}s)")

    last_sync = 0.0
    while True:
        # 取り込む銘柄が変わったら（戦略の追加・有効化・監視銘柄の変更）quotes シートを合わせる
        if not args.simulate and time.time() - last_sync >= QUOTE_SYNC_SEC:
            last_sync = time.time()
            try:
                codes = fetch_quote_codes(client, quote_codes_url)
                if codes and sync_quote_sheet(args.workbook, codes):
                    print(f"{datetime.now():%H:%M:%S} [quotes] 取り込み銘柄を更新: {codes}")
            except Exception as e:  # noqa: BLE001
                print(f"[warn][quotes] {e}")
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
