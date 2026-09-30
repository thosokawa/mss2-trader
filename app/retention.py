"""古い tick（bridge から届く生の気配）を定期的に消す。

tick は2秒ほどごとに銘柄ごとに1行増え、3週間で約50万行になる。足（Bar）は tick から作り終えて
別テーブルに残るので、古い tick は画面にも売買にも使わない。config の app.tick_retention_days
（既定30日）より古いものを消す。各銘柄の最新1件だけは日付に関係なく残す（しばらく受信していない
銘柄でも「最後の株価」を表示できるように）。

DB を長く握らないよう、少しずつ（BATCH 行ずつ）別々のトランザクションで消す。
消した分の領域は SQLite が再利用するので、ファイルサイズは増えなくなる（縮めるには VACUUM）。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import text

from app.models import utcnow

log = logging.getLogger("retention")

BATCH = 20000

_last_run: datetime | None = None
RUN_EVERY = timedelta(hours=6)


def prune_ticks(engine, days: int, now: datetime | None = None) -> int:
    """days 日より古い tick を消す（各銘柄の最新1件は残す）。消した行数を返す。"""
    if days <= 0:
        return 0
    cutoff = (now or utcnow()) - timedelta(days=days)
    total = 0
    while True:
        with engine.begin() as conn:
            n = conn.execute(
                text(
                    "DELETE FROM tick WHERE id IN ("
                    " SELECT id FROM tick WHERE ts < :cutoff"
                    " AND id NOT IN (SELECT MAX(id) FROM tick GROUP BY symbol_code)"
                    " LIMIT :batch)"
                ),
                {"cutoff": cutoff, "batch": BATCH},
            ).rowcount
        total += n or 0
        if not n or n < BATCH:
            break
    if total:
        log.info("古い tick を %d 行削除（%d 日より前）", total, days)
    return total


def maybe_prune(engine, days: int, in_session: bool, now: datetime | None = None) -> int:
    """取引時間外に、前回から RUN_EVERY 以上たっていれば prune_ticks を実行する。"""
    global _last_run
    now = now or utcnow()
    if days <= 0 or in_session:
        return 0
    if _last_run is not None and now - _last_run < RUN_EVERY:
        return 0
    _last_run = now
    return prune_ticks(engine, days, now)
