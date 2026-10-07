"""The morning job: Sunday makes the week plan, other days re-check the weather, then the image is pre-rendered."""

import datetime as dt
import logging
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select

from app.config import get_settings
from app.db import get_sessionmaker
from app.models import Garden
from app.services.llm import LLM
from app.services.plans import create_week_plan, current_week_plan, garden_now, refresh_today
from app.services.today import ensure_plan_image, load_today

log = logging.getLogger(__name__)

SUNDAY = 6


def job_id(garden_id: int) -> str:
    return f"morning-{garden_id}"


async def morning_job(garden_id: int, now: dt.datetime | None = None, llm: LLM | None = None) -> str:
    """Returns what it did, for logs and tests."""
    async with get_sessionmaker()() as session:
        garden = await session.get(Garden, garden_id)
        if garden is None:
            return "no garden"
        now = now or garden_now(garden)
        today = now.date()
        try:
            if today.weekday() == SUNDAY or await current_week_plan(session, garden, today) is None:
                await create_week_plan(session, garden, today=today, llm=llm)
                outcome = "week plan"
            else:
                changes = await refresh_today(session, garden, today=today, llm=llm)
                outcome = f"refreshed ({', '.join(changes) or 'no change'})"
            await ensure_plan_image(await load_today(session, garden, now))
        except Exception:  # keep the scheduler alive; tomorrow is another day
            log.exception("Morning job failed for garden %s", garden_id)
            return "failed"
    log.info("Morning job for %s: %s", garden.name, outcome)
    return outcome


async def plan_now(garden_id: int, llm: LLM | None = None) -> None:
    """Background job for the setup page's "Plan this week now" button."""
    async with get_sessionmaker()() as session:
        garden = await session.get(Garden, garden_id)
        if garden is None:
            return
        try:
            await create_week_plan(session, garden, llm=llm)
            await ensure_plan_image(await load_today(session, garden, garden_now(garden)))
        except Exception:
            log.exception("Planning failed for garden %s", garden_id)


def schedule_garden(scheduler: AsyncIOScheduler, garden: Garden) -> None:
    trigger = CronTrigger(hour=get_settings().morning_hour, minute=0, timezone=ZoneInfo(garden.timezone))
    scheduler.add_job(
        morning_job,
        trigger,
        args=[garden.id],
        id=job_id(garden.id),
        replace_existing=True,
        coalesce=True,
        misfire_grace_time=3 * 3600,  # still run if the server was down at 7 AM
    )


async def sync_jobs(scheduler: AsyncIOScheduler) -> int:
    async with get_sessionmaker()() as session:
        gardens = list(await session.scalars(select(Garden)))
    for garden in gardens:
        schedule_garden(scheduler, garden)
    return len(gardens)


def create_scheduler() -> AsyncIOScheduler:
    return AsyncIOScheduler()
