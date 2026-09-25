"""過去足を yfinance から取得して DB に保存する（CLI）。

  python scripts/fetch_history.py 7203 6501 --interval 5m --period 60d
  python scripts/fetch_history.py 7203 --interval 1d --period 2y

同じ処理は Web UI の /data 画面（「過去データを取得」フォーム）からも実行できる。
実体は app/history.py（fetch_one / fetch_and_store）。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlmodel import Session  # noqa: E402

from app.db import engine, init_db  # noqa: E402
from app.history import INTERVAL_MAP, fetch_and_store  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("codes", nargs="+", help="証券コード 例: 7203 6501")
    ap.add_argument("--interval", default="5m", choices=list(INTERVAL_MAP))
    ap.add_argument("--period", default="60d")
    args = ap.parse_args()

    init_db()
    with Session(engine) as s:
        for code in args.codes:
            r = fetch_and_store(s, code, args.interval, args.period)
            if r["ok"]:
                print(f"{code}: {r['n']} 本 保存（{r['start']} 〜 {r['end']}）")
            else:
                print(f"{code}: {r['message']}")


if __name__ == "__main__":
    main()
