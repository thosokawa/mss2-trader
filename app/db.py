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


def get_session() -> Iterator[Session]:
    with Session(engine) as session:
        yield session
