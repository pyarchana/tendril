"""Ask the local model for a plan, validate it strictly, retry once, then fall back to rules."""

import datetime as dt
import logging
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

from app.models import Plant
from app.schemas import DAY_SCHEMA, WEEK_SCHEMA, PlanDay, PlanTask, WeekPlanReply
from app.services.llm import LLM, OllamaClient, ask_validated
from app.services.rules import (
    RULE_KINDS,
    DayContext,
    day_contexts,
    enforce_rules,
    fallback_day,
    health_tasks,
    match_plant,
    required_tasks,
    rule_based_plan,
    task_kind,
)
from app.services.weather import Forecast, format_hour

log = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You are Tendril, a calm, practical garden assistant. You plan a few short care tasks per day "
    "for a home gardener, based on the weather forecast and what they told you. Reply with JSON only."
)

GUIDELINES = """Guidelines:
- Include each "Needed" item on its day, as given.
- Add one light care task per day that suits the plant and the weather, for example: pinch off flower
  spikes, feed with compost, check under leaves for pests, harvest ripe fruit or leaves, loosen the
  topsoil, tie up drooping stems, remove yellow leaves. Vary them, and spread them across all the
  plants over the week.
- Don't add watering or shade tasks unless they are Needed. Treat pests, wilting or rot first.
- At most 2 tasks per day. "plant" is one of the plant names above, written exactly.
- "action" is under 6 words and starts with a verb. "reason" is a few words, in your own words."""

# Small models copy instructions into their answers; these phrases mean a reason was copied.
_ECHOES = ("needed:", "must:", "tasks per day", "under 6 words", "starts with a verb", "own words", "names above")


def _example(plants: Sequence[Plant], date: str) -> str:
    first = plants[0].name if plants else "Tulsi"
    second = plants[1].name if len(plants) > 1 else first
    tasks = (
        f'{{"plant": "{first}", "action": "Water deeply at the roots", "reason": "3 dry days in a row."}}, '
        f'{{"plant": "{second}", "action": "Check under leaves for pests", "reason": "Warm days bring aphids."}}'
    )
    return f'{{"date": "{date}", "tasks": [{tasks}]}}'


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
    rain = f"{w.rain_probability}% rain chance"
    if w.rain_start_hour is not None:
        rain += f", likely from {format_hour(w.rain_start_hour)}"
    line = f"- {w.date.isoformat()} {w.date:%a}: {w.condition}, {w.temp_min:.0f}-{w.temp_max:.0f}°C, {rain}."
    needed = [f"{t.action.lower()} for {t.plant} ({t.reason.rstrip('.')})" for t in required_tasks(context, plants)]
    if needed:
        line += f" Needed: {'; '.join(needed)}."
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
    first_date = contexts[0].weather.date.isoformat() if contexts else "2026-01-01"
    return f"""Plan the next {len(contexts)} days of care for this garden.

Plants:
{_plant_lines(plants)}

Days:
{forecast_lines}

Recent check-ins from the gardener:
{_facts_lines(facts)}

{GUIDELINES}

Example of the format for one day:
{_example(plants, first_date)}

Reply with compact JSON only (no indentation), one entry for every date above:
{{"days": [...]}}"""


def build_today_prompt(
    plants: Sequence[Plant],
    context: DayContext,
    current: Sequence[PlanTask],
    changes: Sequence[str],
    facts: Sequence[dict],
) -> str:
    task_lines = "\n".join(f"- {t.plant}: {t.action} ({t.reason})" for t in current) or "- none"
    date = context.weather.date.isoformat()
    return f"""The forecast for today changed since the plan was made: {", ".join(changes)}.

Plants:
{_plant_lines(plants)}

Today:
{_forecast_line(context, plants)}

Today's planned tasks:
{task_lines}

Recent check-ins from the gardener:
{_facts_lines(facts)}

{GUIDELINES}

Adjust only today's tasks for the new weather. Keep tasks that still make sense.
Reply with compact JSON only (no indentation), like this example:
{_example(plants, date)}"""


def _with_known_plants(day: PlanDay, plants: Sequence[Plant]) -> PlanDay:
    tasks = []
    for task in day.tasks:
        plant = match_plant(task.plant, plants)
        if plant is None:
            names = ", ".join(p.name for p in plants)
            raise ValueError(f"unknown plant '{task.plant}'; use one of: {names}")
        reason = "" if any(echo in task.reason.lower() for echo in _ECHOES) else task.reason
        tasks.append(task.model_copy(update={"plant": plant.name, "reason": reason}))
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


_EXAMPLE_REASON = "warm days bring aphids."
_CONDITION_WORDS = {
    "sunny": "Sunny", "partly": "Partly cloudy", "cloudy": "Cloudy", "rain": "Rainy",
    "storm": "Stormy", "fog": "Foggy", "snow": "Snowy",
}


def weather_note(context: DayContext) -> str:
    w = context.weather
    return f"{_CONDITION_WORDS.get(w.condition, 'Mild')} day, up to {w.temp_max:.0f}°C."


def _replace_copied_reasons(days: Sequence[PlanDay], contexts: Sequence[DayContext]) -> list[PlanDay]:
    """Keep the first use of a reason; later repeats (or the example's reason) become a weather note."""
    seen: set[str] = set()
    result = []
    for day, context in zip(days, contexts):
        tasks = []
        for task in day.tasks:
            reason = task.reason.lower()
            copied = reason in seen or (reason == _EXAMPLE_REASON and "pest" not in task.action.lower())
            seen.add(reason)
            tasks.append(task.model_copy(update={"reason": weather_note(context)}) if copied or not reason else task)
        result.append(PlanDay(date=day.date, tasks=tasks))
    return result


def _limit_repeats(days: Sequence[PlanDay], limit: int = 2) -> list[PlanDay]:
    """Small models repeat themselves; keep each plant's task at most `limit` times a week."""
    seen: Counter[tuple[str, str]] = Counter()
    limited = []
    for day in days:
        kept = []
        for task in day.tasks:
            key = (task.plant, task.action.lower())
            seen[key] += 1
            if seen[key] <= limit or task_kind(task.action) in RULE_KINDS:  # rule tasks are checked per day
                kept.append(task)
        limited.append(PlanDay(date=day.date, tasks=kept))
    return limited


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
    days = await ask_validated(llm or OllamaClient(), messages, WEEK_SCHEMA, lambda d: _validate_week(d, plan_dates, plants))
    if days is None:
        log.warning("Using the rule-based plan instead of the model")
        return PlanResult(rule_based_plan(plants, forecast, facts, plan_dates), "rules")

    health = health_tasks(facts, plants, plan_dates[0])
    enforced = [
        enforce_rules(day, context, plants, health if index == 0 else (), index)
        for index, (day, context) in enumerate(zip(_replace_copied_reasons(_limit_repeats(days), contexts), contexts))
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
    day = await ask_validated(llm or OllamaClient(), messages, DAY_SCHEMA, lambda d: _validate_day(d, date, plants))
    if day is None:
        log.warning("Using the rule-based day instead of the model")
        return PlanResult([fallback_day(context, plants, date.toordinal(), health)], "rules")
    return PlanResult([enforce_rules(day, context, plants, health, date.toordinal())], "model")


def days_to_json(days: Sequence[PlanDay]) -> list[dict]:
    return [day.model_dump(mode="json") for day in days]


def days_from_json(data: Sequence[dict]) -> list[PlanDay]:
    return [PlanDay.model_validate(day) for day in data]
