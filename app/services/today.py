"""What the Today page shows, the plan image that goes with it, and marking today's tasks."""

import asyncio
import datetime as dt
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from PIL import Image
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import CheckIn, Garden, Task, TaskStatus
from app.schemas import MAX_TASKS_PER_DAY, PlanDay, PlanTask
from app.services import planner
from app.services.plans import current_week_plan
from app.services.render import lockscreen_image, render_plan_image, save_plan_image, short_label
from app.services.rules import serious_issues
from app.services.weather import HEAT_TEMP_C, DayForecast, Forecast, format_hour

EVENING_HOUR = 18
UPCOMING_DAYS = 6


def summary_line(cards: list[PlanTask], weather: DayForecast | None) -> str:
    """One line, e.g. "Chillies: skip watering today. Rain at 4 PM." """
    if cards:
        first = cards[0]
        text = f"{first.plant}: {first.action[:1].lower()}{first.action[1:]}."
    else:
        text = "Nothing needed in the garden today."
    if weather is not None:
        if weather.rain_start_hour is not None:
            text += f" Rain at {format_hour(weather.rain_start_hour)}."
        elif weather.temp_max > HEAT_TEMP_C:
            text += f" Up to {weather.temp_max:.0f}°."
    return text


@dataclass
class TodayView:
    garden: Garden
    now: dt.datetime
    has_plan: bool
    weather: DayForecast | None
    tasks: list[Task]
    upcoming: list[tuple[dt.date, DayForecast | None, PlanDay | None]]
    plan_note: str | None = None  # set when today's voice note changed the plan
    plan_source: str | None = None  # "rules" when the backup planner stood in for the model

    @property
    def date(self) -> dt.date:
        return self.now.date()

    @property
    def pending(self) -> list[Task]:
        return [task for task in self.tasks if task.status == TaskStatus.pending]

    @property
    def evening_nudge(self) -> bool:
        """A single gentle reminder after 6 PM while something is still pending."""
        return self.now.hour >= EVENING_HOUR and bool(self.pending)

    @property
    def cards(self) -> list[PlanTask]:
        tasks = self.tasks
        if len(tasks) > MAX_TASKS_PER_DAY:  # after a re-plan, show what is still to do first
            tasks = self.pending + [task for task in tasks if task.status != TaskStatus.pending]
        return [PlanTask(plant=t.plant.name, action=t.action, reason=t.reason) for t in tasks[:MAX_TASKS_PER_DAY]]

    @property
    def summary(self) -> str:
        return summary_line(self.cards, self.weather)

    @property
    def image_key(self) -> str:
        """Changes whenever anything drawn on the image changes."""
        payload = {
            "date": self.date.isoformat(),
            "weather": self.weather.model_dump(mode="json") if self.weather else None,
            "cards": [card.model_dump() for card in self.cards],
            "upcoming": [
                [date.isoformat(), weather.condition if weather else None, short_label(day)]
                for date, weather, day in self.upcoming
            ],
        }
        return hashlib.sha1(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:10]


async def load_today(session: AsyncSession, garden: Garden, now: dt.datetime) -> TodayView:
    today = now.date()
    plan = await current_week_plan(session, garden, today)
    plant_ids = [plant.id for plant in garden.plants]
    tasks = list(
        await session.scalars(select(Task).where(Task.plant_id.in_(plant_ids), Task.date == today).order_by(Task.id))
    )

    forecast = Forecast.model_validate(plan.forecast) if plan else None
    plan_days = {day.date: day for day in planner.days_from_json(plan.days)} if plan else {}
    upcoming = []
    for offset in range(1, UPCOMING_DAYS + 1):
        date = today + dt.timedelta(days=offset)
        upcoming.append((date, forecast.day(date) if forecast else None, plan_days.get(date)))

    return TodayView(
        garden=garden,
        now=now,
        has_plan=plan is not None,
        weather=forecast.day(today) if forecast else None,
        tasks=tasks,
        upcoming=upcoming,
        plan_note=await _plan_note(session, garden, today),
        plan_source=plan.source if plan else None,
    )


async def _plan_note(session: AsyncSession, garden: Garden, today: dt.date) -> str | None:
    check_ins = await session.scalars(
        select(CheckIn).where(CheckIn.garden_id == garden.id, CheckIn.date == today).order_by(CheckIn.id.desc())
    )
    for check_in in check_ins:
        issues = serious_issues(check_in.facts.get("health_flags", []))
        if issues:
            plant = check_in.facts.get("plant")
            if not plant:
                return f"{' and '.join(issues).capitalize()} reported, but on which plant? Send a quick note naming it."
            return f"Plan changed after your voice note: {' and '.join(issues)} on {plant}."
    return None


def _render_to(view: TodayView, path: Path) -> None:
    image = render_plan_image(view.date, view.weather, view.cards, view.upcoming)
    partial = path.with_suffix(".tmp")
    save_plan_image(image, partial)
    partial.replace(path)


async def ensure_plan_image(view: TodayView) -> Path:
    """Render today's image once per distinct content and drop older versions for the day."""
    images = get_settings().images_dir
    prefix = f"garden{view.garden.id}-{view.date.isoformat()}-"
    path = images / f"{prefix}{view.image_key}.png"
    if not path.exists():
        await asyncio.to_thread(_render_to, view, path)
        for old in images.glob(f"{prefix}*.png"):
            if old != path:
                old.unlink(missing_ok=True)
    return path


def _lockscreen_to(plan_path: Path, path: Path) -> None:
    with Image.open(plan_path) as plan:
        image = lockscreen_image(plan.convert("RGB"))
    partial = path.with_suffix(".tmp")
    save_plan_image(image, partial)
    partial.replace(path)


async def ensure_lockscreen_image(view: TodayView) -> Path:
    """The phone-shaped copy of today's plan, rebuilt whenever the plan image changes."""
    plan_path = await ensure_plan_image(view)
    images = get_settings().images_dir
    prefix = f"lock-garden{view.garden.id}-{view.date.isoformat()}-"
    path = images / f"{prefix}{view.image_key}.png"
    if not path.exists():
        await asyncio.to_thread(_lockscreen_to, plan_path, path)
        for old in images.glob(f"{prefix}*.png"):
            if old != path:
                old.unlink(missing_ok=True)
    return path


async def set_today_status(session: AsyncSession, garden: Garden, today: dt.date, status: TaskStatus) -> int:
    """Mark all of today's pending tasks done or skipped. Returns how many changed."""
    plant_ids = [plant.id for plant in garden.plants]
    result = await session.execute(
        update(Task)
        .where(Task.plant_id.in_(plant_ids), Task.date == today, Task.status == TaskStatus.pending)
        .values(status=status, completed_at=dt.datetime.now(dt.timezone.utc))
    )
    await session.commit()
    return result.rowcount
