from __future__ import annotations

from collections.abc import Iterator

from sqlmodel import Session, SQLModel, create_engine

from app.config import ROOT, get_config

_cfg = get_config()

# sqlite:///data/mss2.db を絶対パスに正規化しておく（起動ディレクトリに依存しないため）
_url = _cfg.app.db_url
if _url.startswith("sqlite:///") and not _url.startswith("sqlite:////"):
    rel = _url.replace("sqlite:///", "", 1)
    abs_path = (ROOT / rel).resolve()
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    _url = f"sqlite:///{abs_path}"

engine = create_engine(_url, echo=False, connect_args={"check_same_thread": False})


def init_db() -> None:
    import app.models  # noqa: F401  テーブル登録のため

    SQLModel.metadata.create_all(engine)
    added = _migrate_columns()
    _migrate_symbol_sets(added)


# create_all() は新規テーブルは作るが、既存テーブルへの列追加はしない。
# P0 で空のまま作られていた既存テーブル（order 等）に後から列を足すときはここに追記する。
# (table, column, SQLiteの型) の組。既にあれば何もしない。
_ADD_COLUMNS: list[tuple[str, str, str]] = [
    ("order", "strategy_name", "TEXT"),
    ("order", "account_type", "TEXT"),
    ("order", "reason", "TEXT"),
    ("order", "filled_qty", "INTEGER"),
    ("order", "avg_price", "REAL"),
    ("order", "updated_at", "TIMESTAMP"),
    ("order", "ref_price", "REAL DEFAULT 0"),
    ("papertrade", "side", "TEXT DEFAULT 'LONG'"),
    ("order", "trade_type", "TEXT DEFAULT 'cash'"),
    ("order", "margin_type", "INTEGER DEFAULT 0"),
    ("order", "open_date", "INTEGER DEFAULT 0"),
    ("strategy", "symbols", "TEXT DEFAULT ''"),
    ("symbol", "watch", "BOOLEAN DEFAULT 0"),
    ("strategy", "deleted", "BOOLEAN DEFAULT 0"),
]


def _migrate_columns() -> set[tuple[str, str]]:
    """足りない列を追加する。今回追加した (table, column) を返す（データ移行の判定用）。"""
    added = set()
    with engine.begin() as conn:
        for table, column, sql_type in _ADD_COLUMNS:
            cols = {row[1] for row in conn.exec_driver_sql(f'PRAGMA table_info("{table}")')}
            if column not in cols:
                conn.exec_driver_sql(f'ALTER TABLE "{table}" ADD COLUMN {column} {sql_type}')
                added.add((table, column))
    return added


def _migrate_symbol_sets(added: set[tuple[str, str]]) -> None:
    """銘柄セット廃止に伴う一度きりのデータ移行（列を追加したときだけ走る）。

    - 戦略: 対象の銘柄セットの中身を Strategy.symbols（カンマ区切り）に写す
    - 監視銘柄: どの戦略も使っていない銘柄セット（旧「RSS監視」＝ run_all.ps1 の --set-id 1）の
      銘柄を Symbol.watch=1 にする（株価の取り込みを続けるため）
    """
    with engine.begin() as conn:
        if ("strategy", "symbols") in added:
            rows = conn.exec_driver_sql(
                "SELECT id, symbol_set_id FROM strategy WHERE symbol_set_id IS NOT NULL"
            ).fetchall()
            for sid, set_id in rows:
                codes = [r[0] for r in conn.exec_driver_sql(
                    "SELECT symbol_code FROM symbolsetitem WHERE set_id = ? ORDER BY sort_order, id",
                    (set_id,),
                ).fetchall()]
                conn.exec_driver_sql(
                    "UPDATE strategy SET symbols = ? WHERE id = ?", (",".join(codes), sid)
                )
        if ("symbol", "watch") in added:
            used = {r[0] for r in conn.exec_driver_sql(
                "SELECT DISTINCT symbol_set_id FROM strategy WHERE symbol_set_id IS NOT NULL"
            ).fetchall()}
            for set_id, code in conn.exec_driver_sql(
                "SELECT set_id, symbol_code FROM symbolsetitem"
            ).fetchall():
                if set_id not in used:
                    conn.exec_driver_sql("UPDATE symbol SET watch = 1 WHERE code = ?", (code,))


def get_session() -> Iterator[Session]:
    with Session(engine) as session:
        yield session
