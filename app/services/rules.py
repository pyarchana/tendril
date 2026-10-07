"""Weather rules every plan must follow, and the rule-based planner used when the model fails.

The model is free to suggest tasks, but these rules always win:
- rain probability above 60%: no watering, say to skip it
- max temperature above 35°C: shade advice
- 3+ dry days in a row: deep watering
- serious problems from recent check-ins (pests, wilting, rot) come first
"""

import datetime as dt
import re
from collections.abc import Sequence
from dataclasses import dataclass

from app.models import LocationType, Plant
from app.schemas import MAX_TASKS_PER_DAY, PlanDay, PlanTask
from app.services.weather import DayForecast, Forecast, format_hour

DEEP_WATER_AFTER_DRY_DAYS = 3
HEALTH_LOOKBACK_DAYS = 3
RULE_KINDS = {"skip_water", "shade", "deep_water"}  # only the weather rules decide these

# Pots dry out fastest, open beds slowest.
_THIRST = {LocationType.pot: 0, LocationType.window: 1, LocationType.terrace: 2, LocationType.bed: 3}
_MOVABLE = (LocationType.pot, LocationType.window)

_NOT_WATERING = ("skip", "no ", "don't", "do not", "avoid", "hold off", "pause", "cut back", "reduce", "less")

# issue -> (words that signal it, action, reason)
SERIOUS_ISSUES = {
    "pests": (
        ("pest", "aphid", "mite", "whitefl", "mealybug", "caterpillar", "insect", "bug"),
        "Spray neem oil on leaves",
        "Pests reported in your check-in.",
    ),
    "wilting": (
        ("wilt", "droop"),
        "Check soil, water if dry",
        "Wilting reported in your check-in.",
    ),
    "rot": (
        ("rot", "mould", "mold", "fung"),
        "Cut back on watering",
        "Rot reported; let the soil dry out.",
    ),
}

# Calm, low-effort tasks used to fill quiet days in the fallback plan.
_ROUTINE = [
    ("Check soil moisture", "A quick finger test keeps watering honest."),
    ("Look under leaves for pests", "Pests are easiest to stop early."),
    ("Water lightly in the morning", "A mild day; keep the soil evenly moist."),
    ("Pinch off yellow leaves", "Saves the plant's energy for new growth."),
    ("Loosen the topsoil gently", "Helps water reach the roots."),
]


@dataclass
class DayContext:
    weather: DayForecast
    dry_streak: int  # dry days in a row before this day, reset by rain or deep watering

    @property
    def needs_deep_water(self) -> bool:
        return self.dry_streak >= DEEP_WATER_AFTER_DRY_DAYS and not self.weather.is_rainy


def day_contexts(forecast: Forecast) -> list[DayContext]:
    streak = 0
    for past in forecast.past_days:
        streak = streak + 1 if past.is_dry else 0
    contexts = []
    for day in forecast.days:
        context = DayContext(weather=day, dry_streak=streak)
        contexts.append(context)
        streak = 0 if context.needs_deep_water or not day.is_dry else streak + 1
    return contexts


def is_watering(action: str) -> bool:
    text = action.lower()
    return "water" in text and not any(word in text for word in _NOT_WATERING)


def task_kind(action: str) -> str | None:
    """Rough category of an action, used to avoid adding a rule task the plan already covers."""
    text = action.lower()
    if "water" in text and any(word in text for word in ("skip", "hold off", "pause", "no ", "don't", "do not")):
        return "skip_water"
    if "shade" in text:
        return "shade"
    if "deep" in text and "water" in text:
        return "deep_water"
    if "neem" in text or "spray" in text:
        return "pests"
    if re.search(r"\brot(?:ting|ten)?\b", text) or "cut back" in text:
        return "rot"
    if "wilt" in text or "check soil" in text:
        return "wilting"
    return None


def match_plant(name: str, plants: Sequence[Plant]) -> Plant | None:
    """Find a plant by a name the model or a voice note used ("chilli plant" -> Chillies)."""
    wanted = name.strip().lower()
    if not wanted:
        return None
    for plant in plants:
        if plant.name.lower() == wanted:
            return plant
    wanted_stem = wanted.rstrip("s")
    for plant in plants:
        stem = plant.name.lower().rstrip("s")
        if stem in wanted_stem or wanted_stem in stem or stem.split()[0][:5] == wanted_stem.split()[0][:5]:
            return plant
    return None


def serious_issues(flags: Sequence[str]) -> list[str]:
    found = []
    for issue, (signals, _, _) in SERIOUS_ISSUES.items():
        if any(signal in flag.lower() for flag in flags for signal in signals):
            found.append(issue)
    return found


def _by_thirst(plants: Sequence[Plant]) -> list[Plant]:
    return sorted(plants, key=lambda plant: _THIRST.get(plant.location_type, len(_THIRST)))


def health_tasks(facts: Sequence[dict], plants: Sequence[Plant], first_day: dt.date) -> list[PlanTask]:
    """Treatment tasks for serious problems reported in the last few days."""
    tasks = []
    since = first_day - dt.timedelta(days=HEALTH_LOOKBACK_DAYS)
    for fact in facts:
        try:
            reported = dt.date.fromisoformat(fact.get("date", ""))
        except ValueError:
            continue
        if reported < since:
            continue
        plant = match_plant(fact.get("plant", ""), plants) or _by_thirst(plants)[0]
        for issue in serious_issues(fact.get("health_flags", [])):
            _, action, reason = SERIOUS_ISSUES[issue]
            tasks.append(PlanTask(plant=plant.name, action=action, reason=reason))
    return tasks


def required_tasks(context: DayContext, plants: Sequence[Plant]) -> list[PlanTask]:
    weather = context.weather
    thirsty = _by_thirst(plants)
    tasks = []
    if weather.is_rainy:
        when = f" from {format_hour(weather.rain_start_hour)}" if weather.rain_start_hour is not None else ""
        tasks.append(
            PlanTask(
                plant=thirsty[0].name,
                action="Skip watering today",
                reason=f"{weather.rain_probability}% chance of rain{when}.",
            )
        )
    if weather.is_hot:
        plant = next((p for p in thirsty if p.location_type in _MOVABLE), thirsty[0])
        action = "Move pot into afternoon shade" if plant.location_type in _MOVABLE else "Shade it from noon sun"
        tasks.append(PlanTask(plant=plant.name, action=action, reason=f"Heat peaks at {weather.temp_max:.0f}°C."))
    if context.needs_deep_water:
        tasks.append(
            PlanTask(
                plant=thirsty[0].name,
                action="Water deeply at the roots",
                reason=f"{context.dry_streak} dry days in a row.",
            )
        )
    return tasks


def _merge(first: list[PlanTask], then: list[PlanTask]) -> list[PlanTask]:
    """Rule tasks first unless the plan already covers them; drop duplicates; cap per day."""
    covered = {task_kind(task.action) for task in then} - {None}
    merged: list[PlanTask] = []
    seen = set()
    for task in [t for t in first if task_kind(t.action) not in covered] + then:
        key = (task.plant.lower(), task.action.lower())
        if key not in seen:
            seen.add(key)
            merged.append(task)
    return merged[:MAX_TASKS_PER_DAY]


def _add_routine(
    tasks: list[PlanTask], context: DayContext, plants: Sequence[Plant], index: int, upto: int
) -> list[PlanTask]:
    """Top a day up with calm routine tasks, rotating so neighbouring days differ."""
    tasks = list(tasks)
    busy = {task.plant for task in tasks}
    for step in range(len(_ROUTINE)):
        if len(tasks) >= upto:
            break
        # Each day can use up to two routine tasks, so advance by two to avoid repeats.
        action, reason = _ROUTINE[(index * MAX_TASKS_PER_DAY + step) % len(_ROUTINE)]
        if is_watering(action) and (context.weather.is_rainy or any(is_watering(t.action) for t in tasks)):
            continue
        free = [p for p in plants if p.name not in busy] or list(plants)
        plant = free[(index + step) % len(free)]
        if is_watering(action) and "water" in (plant.notes or "").lower():
            continue  # the gardener already has a watering routine for this plant
        busy.add(plant.name)
        tasks.append(PlanTask(plant=plant.name, action=action, reason=reason))
    return tasks


def enforce_rules(
    day: PlanDay, context: DayContext, plants: Sequence[Plant], extra: Sequence[PlanTask] = (), index: int = 0
) -> PlanDay:
    """Make a model-made day obey the weather rules.

    The rules decide when skip-watering, shade and deep-watering happen: the model's versions are
    dropped on other days, and when they match a rule they take its reason ("3 dry days in a row.").
    A day left empty gets one routine task.
    """
    required = [*extra, *required_tasks(context, plants)]
    rule_reasons = {task_kind(t.action): t.reason for t in required if task_kind(t.action)}
    watered = {p.name for p in plants if "water" in (p.notes or "").lower()}  # already on a routine
    tasks = []
    for task in day.tasks:
        kind = task_kind(task.action)
        if context.weather.is_rainy and is_watering(task.action):
            continue
        if kind in RULE_KINDS and kind not in rule_reasons:
            continue
        if kind is None and is_watering(task.action) and task.plant in watered:
            continue
        reason = rule_reasons.get(kind)
        tasks.append(task.model_copy(update={"reason": reason}) if reason else task)
    merged = _merge(required, tasks) or _add_routine([], context, plants, index, upto=1)
    return PlanDay(date=day.date, tasks=merged)


def fallback_day(
    context: DayContext, plants: Sequence[Plant], index: int, extra: Sequence[PlanTask] = ()
) -> PlanDay:
    tasks = _merge([*extra, *required_tasks(context, plants)], [])
    return PlanDay(date=context.weather.date, tasks=_add_routine(tasks, context, plants, index, MAX_TASKS_PER_DAY))


def rule_based_plan(
    plants: Sequence[Plant], forecast: Forecast, facts: Sequence[dict], dates: Sequence[dt.date] | None = None
) -> list[PlanDay]:
    contexts = [c for c in day_contexts(forecast) if dates is None or c.weather.date in dates]
    if not contexts or not plants:
        return [PlanDay(date=c.weather.date) for c in contexts]
    health = health_tasks(facts, plants, contexts[0].weather.date)
    return [
        fallback_day(context, plants, index, health if index == 0 else ())
        for index, context in enumerate(contexts)
    ]
