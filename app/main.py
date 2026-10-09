import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.config import get_settings
from app.db import dispose_engine, init_db
from app.deps import preload_whisper
from app.ratelimit import RateLimiter
from app.routers import checkin, setup, tasks, today
from app.scheduler import create_scheduler, schedule_housekeeping, sync_jobs

STATIC_DIR = Path(__file__).resolve().parent / "static"
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    logging.basicConfig(level=settings.log_level)
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per outgoing request is noise
    settings.ensure_dirs()
    await init_db()
    app.state.scheduler = None
    if settings.scheduler_enabled:
        app.state.scheduler = create_scheduler()
        app.state.scheduler.start()
        count = await sync_jobs(app.state.scheduler)
        schedule_housekeeping(app.state.scheduler)
        log.info("Scheduler started for %d garden(s), mornings at %d:00", count, settings.morning_hour)
    if settings.whisper_preload:
        preload_whisper()
    if not settings.setup_password:
        log.warning("SETUP_PASSWORD is not set: /setup is open to anyone who can reach this server")
    yield
    if app.state.scheduler:
        app.state.scheduler.shutdown(wait=False)
    await dispose_engine()


def create_app() -> FastAPI:
    app = FastAPI(title="Tendril", lifespan=lifespan)
    app.state.scheduler = None
    app.state.checkin_limiter = RateLimiter()
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(setup.router)
    app.include_router(today.router)
    app.include_router(tasks.router)
    app.include_router(checkin.router)

    @app.get("/", include_in_schema=False)
    async def index() -> RedirectResponse:
        return RedirectResponse("/setup")

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    return app


app = create_app()
