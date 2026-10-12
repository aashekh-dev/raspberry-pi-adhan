"""adhand — LAN adhan daemon + web app.

Run with: uvicorn main:app --host 0.0.0.0 --port 8000
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

import db, scheduler
from routes import router
from config import WEB_DIR
from player import player


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    await scheduler.wait_for_clock_sync()
    scheduler.start()
    try:
        yield
    finally:
        scheduler.shutdown()
        await player.stop()


app = FastAPI(title="adhand", lifespan=lifespan)
app.include_router(router, prefix="/api")
app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
