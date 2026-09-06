from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from sqlmodel import Session

from app.aggregator import build_bars
from app.config import get_config
from app.db import engine, init_db
from app.engine import live
from app.web.routes import router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("main")


async def _aggregator_loop() -> None:
    cfg = get_config().app
    tfs = tuple(cfg.agg_timeframes)
    loop = asyncio.get_running_loop()
    while True:
        await asyncio.sleep(cfg.agg_interval_sec)
        try:
            def _run() -> int:
                with Session(engine) as s:
                    return build_bars(s, timeframes=tfs)

            n = await loop.run_in_executor(None, _run)
            if n:
                log.info("aggregator: %d bars upserted", n)
        except Exception:  # noqa: BLE001
            log.exception("aggregator loop error")


async def _live_loop() -> None:
    cfg = get_config().app
    loop = asyncio.get_running_loop()
    while True:
        await asyncio.sleep(cfg.live_interval_sec)
        try:
            def _run() -> int:
                with Session(engine) as s:
                    return len(live.run_once(s))

            n = await loop.run_in_executor(None, _run)
            if n:
                log.info("live: %d signal(s) fired", n)
        except Exception:  # noqa: BLE001
            log.exception("live loop error")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    tasks: list[asyncio.Task] = []
    if get_config().app.agg_interval_sec > 0:
        tasks.append(asyncio.create_task(_aggregator_loop()))
    if get_config().app.live_interval_sec > 0:
        tasks.append(asyncio.create_task(_live_loop()))
    try:
        yield
    finally:
        for t in tasks:
            t.cancel()


app = FastAPI(title="mss2-trader", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "web" / "static")), name="static")
app.include_router(router)


def main() -> None:
    import os

    import uvicorn

    # 自動起動（run_all.ps1）では MSS2_RELOAD=0 を渡してリローダを止める
    reload = os.environ.get("MSS2_RELOAD", "1").lower() not in ("0", "false", "no", "")
    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=reload)


if __name__ == "__main__":
    main()
