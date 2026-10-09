"""Store plans and tasks: the weekly plan, the daily refresh, and re-planning the rest of the week."""

import asyncio
import datetime as dt
import logging
import weakref
from collections.abc import Sequence
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CheckIn, Garden, Task, TaskStatus, WeekPlan
from app.schemas import PlanDay
from app.services import planner
from app.services.llm import LLM
from app.services.rules import match_plant
from app.services.weather import Forecast, detect_change, fetch_forecast

log = logging.getLogger(__name__)

FACTS_LOOKBACK_DAYS = 14

# One plan change per garden at a time: the 7 AM job, "Plan this week now" and a re-plan after a
# voice note can overlap, and two writers at once would duplicate tasks. Locks belong to their
# event loop, so they're kept per loop.
_locks: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[int, asyncio.Lock]]" = weakref.WeakKeyDictionary()


def garden_lock(garden_id: int) -> asyncio.Lock:
    per_loop = _locks.setdefault(asyncio.get_running_loop(), {})
    return per_loop.setdefault(garden_id, asyncio.Lock())


def garden_now(garden: Garden) -> dt.datetime:
    return dt.datetime.now(ZoneInfo(garden.timezone))


def garden_today(garden: Garden) -> dt.date:
    return garden_now(garden).date()


async def recent_facts(session: AsyncSession, garden: Garden, today: dt.date) -> list[dict]:
    """Facts from the last two weeks of voice check-ins, oldest first."""
    since = today - dt.timedelta(days=FACTS_LOOKBACK_DAYS)
    check_ins = await session.scalars(
        select(CheckIn)
        .where(CheckIn.garden_id == garden.id, CheckIn.date >= since, CheckIn.date <= today)
        .order_by(CheckIn.date, CheckIn.id)
    )
    return [{"date": c.date.isoformat(), **c.facts} for c in check_ins if c.facts]


async def current_week_plan(session: AsyncSession, garden: Garden, today: dt.date) -> WeekPlan | None:
    return await session.scalar(
        select(WeekPlan)
        .where(
            WeekPlan.garden_id == garden.id,
            WeekPlan.week_start <= today,
            WeekPlan.week_start > today - dt.timedelta(days=7),
        )
        .order_by(WeekPlan.week_start.desc())
        .limit(1)
    )


def _merge_forecast(old: Forecast, new: Forecast) -> Forecast:
    """The plan's days updated with fresher forecasts, plus any newly forecast days after them."""
    merged = {day.date: day for day in old.days} | {day.date: day for day in new.days}
    return old.model_copy(update={"days": sorted(merged.values(), key=lambda day: day.date)})


async def _replace_tasks(session: AsyncSession, garden: Garden, days: Sequence[PlanDay]) -> None:
    """Swap pending tasks on these dates for the new plan; finished tasks are kept."""
    plant_ids = [plant.id for plant in garden.plants]
    dates = [day.date for day in days]
    if not plant_ids or not dates:
        return
    await session.execute(
        delete(Task).where(Task.plant_id.in_(plant_ids), Task.date.in_(dates), Task.status == TaskStatus.pending)
    )
    finished = await session.scalars(select(Task).where(Task.plant_id.in_(plant_ids), Task.date.in_(dates)))
    already = {(task.plant_id, task.date, task.action.lower()) for task in finished}
    for day in days:
        for planned in day.tasks:
            plant = match_plant(planned.plant, garden.plants)
            if plant is None or (plant.id, day.date, planned.action.lower()) in already:
                continue
            session.add(Task(plant_id=plant.id, date=day.date, action=planned.action, reason=planned.reason))


async def _create_week_plan(
    session: AsyncSession,
    garden: Garden,
    *,
    today: dt.date | None = None,
    forecast: Forecast | None = None,
    llm: LLM | None = None,
) -> WeekPlan:
    """Plan the next 7 days from a fresh forecast. Re-running on the same day replaces the plan."""
    today = today or garden_today(garden)
    forecast = forecast or await fetch_forecast(garden.lat, garden.lon, garden.timezone)
    facts = await recent_facts(session, garden, today)
    result = await planner.plan_week(garden.plants, forecast, facts, llm)

    plan = await session.scalar(select(WeekPlan).where(WeekPlan.garden_id == garden.id, WeekPlan.week_start == today))
    if plan is None:
        plan = WeekPlan(garden_id=garden.id, week_start=today)
        session.add(plan)
    plan.forecast = forecast.model_dump(mode="json")
    plan.days = planner.days_to_json(result.days)
    plan.source = result.source
    await _replace_tasks(session, garden, result.days)
    await session.commit()
    log.info("Week plan for %s from %s made by %s", garden.name, today, result.source)
    return plan


async def _refresh_today(
    session: AsyncSession,
    garden: Garden,
    *,
    today: dt.date | None = None,
    forecast: Forecast | None = None,
    llm: LLM | None = None,
) -> list[str]:
    """Re-check today's weather; adjust only today's tasks if it changed meaningfully.

    Returns the changes found (empty when nothing changed).
    """
    today = today or garden_today(garden)
    plan = await current_week_plan(session, garden, today)
    if plan is None:
        await _create_week_plan(session, garden, today=today, forecast=forecast, llm=llm)
        return ["new plan"]

    forecast = forecast or await fetch_forecast(garden.lat, garden.lon, garden.timezone)
    current = forecast.day(today)
    if current is None:
        return []
    stored = Forecast.model_validate(plan.forecast)
    planned = stored.day(today)
    changes = detect_change(planned, current) if planned else ["no forecast for today"]
    plan.forecast = _merge_forecast(stored, forecast).model_dump(mode="json")
    if not changes:
        await session.commit()
        return []

    days = planner.days_from_json(plan.days)
    today_plan = next((day for day in days if day.date == today), PlanDay(date=today))
    facts = await recent_facts(session, garden, today)
    result = await planner.adjust_today(garden.plants, forecast, today, today_plan.tasks, changes, facts, llm)
    new_day = result.days[0]
    if result.source == "rules":
        plan.source = "rules"  # so the Today page says the backup planner stepped in

    others = [day for day in days if day.date != today]
    plan.days = planner.days_to_json(sorted([*others, new_day], key=lambda day: day.date))
    await _replace_tasks(session, garden, [new_day])
    await session.commit()
    log.info("Adjusted today's tasks for %s: %s", garden.name, ", ".join(changes))
    return changes


async def _replan_rest_of_week(
    session: AsyncSession,
    garden: Garden,
    *,
    today: dt.date | None = None,
    forecast: Forecast | None = None,
    llm: LLM | None = None,
) -> WeekPlan:
    """Re-plan from today to the end of the current week, e.g. after a serious problem is reported."""
    today = today or garden_today(garden)
    plan = await current_week_plan(session, garden, today)
    if plan is None:
        return await _create_week_plan(session, garden, today=today, forecast=forecast, llm=llm)

    forecast = forecast or await fetch_forecast(garden.lat, garden.lon, garden.timezone)
    week_end = plan.week_start + dt.timedelta(days=6)
    dates = [day.date for day in forecast.days if today <= day.date <= week_end]
    facts = await recent_facts(session, garden, today)
    result = await planner.plan_week(garden.plants, forecast, facts, llm, dates=dates)

    earlier = [day for day in planner.days_from_json(plan.days) if day.date < today]
    plan.days = planner.days_to_json([*earlier, *result.days])
    plan.forecast = _merge_forecast(Forecast.model_validate(plan.forecast), forecast).model_dump(mode="json")
    plan.source = result.source
    await _replace_tasks(session, garden, result.days)
    await session.commit()
    log.info("Re-planned %s from %s by %s", garden.name, today, result.source)
    return plan


async def create_week_plan(
    session: AsyncSession,
    garden: Garden,
    *,
    today: dt.date | None = None,
    forecast: Forecast | None = None,
    llm: LLM | None = None,
) -> WeekPlan:
    async with garden_lock(garden.id):
        return await _create_week_plan(session, garden, today=today, forecast=forecast, llm=llm)


async def refresh_today(
    session: AsyncSession,
    garden: Garden,
    *,
    today: dt.date | None = None,
    forecast: Forecast | None = None,
    llm: LLM | None = None,
) -> list[str]:
    async with garden_lock(garden.id):
        return await _refresh_today(session, garden, today=today, forecast=forecast, llm=llm)


async def replan_rest_of_week(
    session: AsyncSession,
    garden: Garden,
    *,
    today: dt.date | None = None,
    forecast: Forecast | None = None,
    llm: LLM | None = None,
) -> WeekPlan:
    async with garden_lock(garden.id):
        return await _replan_rest_of_week(session, garden, today=today, forecast=forecast, llm=llm)
