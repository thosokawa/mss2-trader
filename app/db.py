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
    _migrate_columns()


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
]


def _migrate_columns() -> None:
    with engine.begin() as conn:
        for table, column, sql_type in _ADD_COLUMNS:
            cols = {row[1] for row in conn.exec_driver_sql(f'PRAGMA table_info("{table}")')}
            if column not in cols:
                conn.exec_driver_sql(f'ALTER TABLE "{table}" ADD COLUMN {column} {sql_type}')


def get_session() -> Iterator[Session]:
    with Session(engine) as session:
        yield session
