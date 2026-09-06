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


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    task: asyncio.Task | None = None
    if get_config().app.agg_interval_sec > 0:
        task = asyncio.create_task(_aggregator_loop())
    try:
        yield
    finally:
        if task:
            task.cancel()


app = FastAPI(title="mss2-trader", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "web" / "static")), name="static")
app.include_router(router)


def main() -> None:
    import uvicorn

    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=True)


if __name__ == "__main__":
    main()
