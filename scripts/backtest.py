"""CLI からバックテストを実行する（Web UI と同じ run_backtest を呼ぶ）。

  python scripts/backtest.py 7203 --interval 5m --fast 5 --slow 20 --qty 100
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlmodel import Session  # noqa: E402

from app.bars import load_bars  # noqa: E402
from app.db import engine, init_db  # noqa: E402
from app.engine.backtest import run_backtest  # noqa: E402
from app.strategy.examples.sma_cross import SmaCross  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("code")
    ap.add_argument("--interval", default="5m")
    ap.add_argument("--fast", type=int, default=5)
    ap.add_argument("--slow", type=int, default=20)
    ap.add_argument("--qty", type=int, default=100)
    ap.add_argument("--commission", type=float, default=0.0)
    args = ap.parse_args()

    init_db()
    with Session(engine) as s:
        bars = load_bars(s, args.code, args.interval)
    if bars.empty:
        print("足データがありません。先に scripts/fetch_history.py を実行してください。")
        return

    strat = SmaCross({"fast": args.fast, "slow": args.slow, "qty": args.qty})
    strat.timeframe = args.interval
    res = run_backtest(strat, bars, args.code, commission_per_trade=args.commission)

    print(f"=== {args.code} / {args.interval}  ({bars.index[0]} 〜 {bars.index[-1]}, {len(bars)}本) ===")
    for k, v in res.metrics.items():
        print(f"  {k}: {v}")
    print(f"  取引 {len(res.trades)} 件")
    for t in res.trades:
        print(
            f"   {t.entry_ts:%m/%d %H:%M} {t.entry_price:.1f} -> "
            f"{t.exit_ts:%m/%d %H:%M} {t.exit_price:.1f}  "
            f"pnl={t.pnl:,.0f} ({t.return_pct:+.2f}%)"
        )


if __name__ == "__main__":
    main()
