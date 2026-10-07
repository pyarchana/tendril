"""Ask the local model for a plan, validate it strictly, retry once, then fall back to rules."""

import datetime as dt
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TypeVar

from app.models import Plant
from app.schemas import DAY_SCHEMA, WEEK_SCHEMA, PlanDay, PlanTask, WeekPlanReply
from app.services.llm import LLM, LLMError, OllamaClient, parse_json_reply
from app.services.rules import (
    DayContext,
    day_contexts,
    enforce_rules,
    fallback_day,
    health_tasks,
    match_plant,
    required_tasks,
    rule_based_plan,
)
from app.services.weather import Forecast, format_hour

log = logging.getLogger(__name__)

T = TypeVar("T")

SYSTEM_PROMPT = (
    "You are Tendril, a calm, practical garden assistant. You plan a few short care tasks per day "
    "for a home gardener, based on the weather forecast and what they told you. Reply with JSON only."
)

RULES_TEXT = """Rules:
- At most 2 tasks per day; fewer is fine on quiet days.
- Each action is under 6 words and starts with a verb, e.g. "Water deeply at the roots".
- Rain chance above 60%: never water that day; tell them to skip watering.
- Max temperature above 35°C: give shade advice.
- After 3 or more dry days in a row: water deeply.
- Deal with any pests, wilting or rot from check-ins first.
- Each reason is one short sentence that cites the weather or a check-in."""

TASK_SHAPE = '{"plant": "<plant name>", "action": "<short action>", "reason": "<why>"}'


@dataclass
class PlanResult:
    days: list[PlanDay]
    source: str  # "model" or "rules"


def _plant_lines(plants: Sequence[Plant]) -> str:
    lines = []
    for plant in plants:
        species = f" ({plant.species})" if plant.species else ""
        notes = f" Notes: {plant.notes}" if plant.notes else ""
        lines.append(f"- {plant.name}{species}, {plant.location_type}.{notes}")
    return "\n".join(lines)


def _forecast_line(context: DayContext, plants: Sequence[Plant]) -> str:
    w = context.weather
    rain_time = f" from {format_hour(w.rain_start_hour)}" if w.rain_start_hour is not None else ""
    line = (
        f"- {w.date.isoformat()} ({w.date:%a}): {w.condition}, {w.temp_min:.0f}-{w.temp_max:.0f}°C, "
        f"rain chance {w.rain_probability}% ({w.rain_mm} mm){rain_time}; dry days before: {context.dry_streak}"
    )
    musts = [f"{t.action.lower()} ({t.plant})" for t in required_tasks(context, plants)]
    if musts:
        line += f". Must: {'; '.join(musts)}"
    return line


def _facts_lines(facts: Sequence[dict]) -> str:
    if not facts:
        return "- none"
    lines = []
    for fact in facts:
        parts = [f"{fact.get('date', '?')}, {fact.get('plant') or 'garden'}"]
        for key, label in (("done", "done"), ("observations", "observed"), ("health_flags", "problems")):
            if fact.get(key):
                parts.append(f"{label}: {', '.join(fact[key])}")
        lines.append("- " + "; ".join(parts))
    return "\n".join(lines)


def build_week_prompt(plants: Sequence[Plant], contexts: Sequence[DayContext], facts: Sequence[dict]) -> str:
    forecast_lines = "\n".join(_forecast_line(c, plants) for c in contexts)
    return f"""Plan the next {len(contexts)} days of care for this garden.

Plants (use these exact names):
{_plant_lines(plants)}

Forecast:
{forecast_lines}

Recent check-ins from the gardener:
{_facts_lines(facts)}

{RULES_TEXT}

Reply with JSON only, in exactly this shape, with one entry for every date above:
{{"days": [{{"date": "YYYY-MM-DD", "tasks": [{TASK_SHAPE}]}}]}}"""


def build_today_prompt(
    plants: Sequence[Plant],
    context: DayContext,
    current: Sequence[PlanTask],
    changes: Sequence[str],
    facts: Sequence[dict],
) -> str:
    task_lines = "\n".join(f"- {t.plant}: {t.action} ({t.reason})" for t in current) or "- none"
    return f"""The forecast for today changed since the plan was made: {", ".join(changes)}.

Plants (use these exact names):
{_plant_lines(plants)}

Today's new forecast:
{_forecast_line(context, plants)}

Today's planned tasks:
{task_lines}

Recent check-ins from the gardener:
{_facts_lines(facts)}

{RULES_TEXT}

Adjust only today's tasks for the new weather. Keep tasks that still make sense.
Reply with JSON only, in exactly this shape:
{{"date": "{context.weather.date.isoformat()}", "tasks": [{TASK_SHAPE}]}}"""


def _with_known_plants(day: PlanDay, plants: Sequence[Plant]) -> PlanDay:
    tasks = []
    for task in day.tasks:
        plant = match_plant(task.plant, plants)
        if plant is None:
            names = ", ".join(p.name for p in plants)
            raise ValueError(f"unknown plant '{task.plant}'; use one of: {names}")
        tasks.append(task.model_copy(update={"plant": plant.name}))
    return PlanDay(date=day.date, tasks=tasks)


def _validate_week(data: dict, dates: Sequence[dt.date], plants: Sequence[Plant]) -> list[PlanDay]:
    reply = WeekPlanReply.model_validate(data)
    by_date: dict[dt.date, PlanDay] = {}
    for day in reply.days:
        if day.date not in dates:
            raise ValueError(f"unexpected date {day.date}")
        by_date[day.date] = _with_known_plants(day, plants)
    missing = [d.isoformat() for d in dates if d not in by_date]
    if missing:
        raise ValueError(f"missing dates: {', '.join(missing)}")
    return [by_date[d] for d in dates]


def _validate_day(data: dict, date: dt.date, plants: Sequence[Plant]) -> PlanDay:
    data = {**data, "date": date.isoformat()}  # only today is being adjusted
    return _with_known_plants(PlanDay.model_validate(data), plants)


async def _ask(llm: LLM, messages: list[dict], schema: dict, validate: Callable[[dict], T]) -> T | None:
    """Two attempts; the second one is told what was wrong with the first."""
    for attempt in (1, 2):
        try:
            reply = await llm.chat_json(messages, schema)
        except LLMError as exc:
            log.warning("Model call failed (attempt %d): %s", attempt, exc)
            continue
        try:
            return validate(parse_json_reply(reply))
        except ValueError as exc:  # includes JSON decode errors and pydantic ValidationError
            problem = str(exc)[:500]
            log.info("Model reply invalid (attempt %d): %s", attempt, problem)
            messages = [
                *messages,
                {"role": "assistant", "content": reply},
                {"role": "user", "content": f"That reply was invalid: {problem}\nReply again with only valid JSON in the required shape."},
            ]
    return None


async def plan_week(
    plants: Sequence[Plant],
    forecast: Forecast,
    facts: Sequence[dict],
    llm: LLM | None = None,
    dates: Sequence[dt.date] | None = None,
) -> PlanResult:
    """Plan the forecast days (or just `dates`). Never raises for model problems."""
    contexts = [c for c in day_contexts(forecast) if dates is None or c.weather.date in dates]
    if not plants or not contexts:
        return PlanResult([PlanDay(date=c.weather.date) for c in contexts], "rules")

    plan_dates = [c.weather.date for c in contexts]
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": build_week_prompt(plants, contexts, facts)},
    ]
    days = await _ask(llm or OllamaClient(), messages, WEEK_SCHEMA, lambda d: _validate_week(d, plan_dates, plants))
    if days is None:
        log.warning("Using the rule-based plan instead of the model")
        return PlanResult(rule_based_plan(plants, forecast, facts, plan_dates), "rules")

    health = health_tasks(facts, plants, plan_dates[0])
    enforced = [
        enforce_rules(day, context, plants, health if index == 0 else ())
        for index, (day, context) in enumerate(zip(days, contexts))
    ]
    return PlanResult(enforced, "model")


async def adjust_today(
    plants: Sequence[Plant],
    forecast: Forecast,
    date: dt.date,
    current: Sequence[PlanTask],
    changes: Sequence[str],
    facts: Sequence[dict],
    llm: LLM | None = None,
) -> PlanResult:
    """Re-plan a single day after a meaningful forecast change."""
    context = next(c for c in day_contexts(forecast) if c.weather.date == date)
    health = health_tasks(facts, plants, date)
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": build_today_prompt(plants, context, current, changes, facts)},
    ]
    day = await _ask(llm or OllamaClient(), messages, DAY_SCHEMA, lambda d: _validate_day(d, date, plants))
    if day is None:
        log.warning("Using the rule-based day instead of the model")
        return PlanResult([fallback_day(context, plants, date.toordinal(), health)], "rules")
    return PlanResult([enforce_rules(day, context, plants, health)], "model")


def days_to_json(days: Sequence[PlanDay]) -> list[dict]:
    return [day.model_dump(mode="json") for day in days]


def days_from_json(data: Sequence[dict]) -> list[PlanDay]:
    return [PlanDay.model_validate(day) for day in data]
