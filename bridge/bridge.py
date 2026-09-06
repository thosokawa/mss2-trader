"""RSS ブリッジ本体。

「価格を運ぶだけ」の薄い層。売買ロジックは持たない。

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
    args = ap.parse_args()

    if args.dump:
        dump_workbook(args.workbook)
        return

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
        if args.once:
            break
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
