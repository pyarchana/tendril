import datetime as dt

from app.schemas import MAX_ACTION_WORDS, MAX_TASKS_PER_DAY, PlanDay, PlanTask
from app.services.rules import (
    day_contexts,
    enforce_rules,
    health_tasks,
    is_watering,
    match_plant,
    rule_based_plan,
    serious_issues,
)
from tests.factories import HOT, MILD, RAINY, START, WET, forecast, plants


def actions(day: PlanDay) -> list[str]:
    return [task.action for task in day.tasks]


def test_rainy_day_skips_watering_with_rain_time():
    plan = rule_based_plan(plants(), forecast(RAINY), facts=[])
    day = plan[0]
    assert "Skip watering today" in actions(day)
    assert not any(is_watering(a) for a in actions(day))
    skip = next(t for t in day.tasks if t.action == "Skip watering today")
    assert skip.plant == "Chillies"  # the pot dries fastest
    assert skip.reason == "85% chance of rain from 4 PM."


def test_hot_day_gets_shade_advice_for_movable_plant():
    day = rule_based_plan(plants(), forecast(HOT), facts=[])[0]
    shade = next(t for t in day.tasks if "shade" in t.action.lower())
    assert shade.plant == "Chillies"
    assert shade.action == "Move pot into afternoon shade"
    assert "38°C" in shade.reason


def test_deep_watering_after_three_dry_days():
    plan = rule_based_plan(plants(), forecast(MILD, MILD, MILD, MILD, past=(MILD, MILD, MILD)), facts=[])
    assert "Water deeply at the roots" in actions(plan[0])
    assert plan[0].tasks[0].reason == "3 dry days in a row."
    # Deep watering resets the dry streak, so the next deep watering is 4 days later.
    assert ["Water deeply at the roots" in actions(d) for d in plan] == [True, False, False, False]


def test_no_deep_watering_when_rain_is_due():
    contexts = day_contexts(forecast(RAINY, past=(MILD, MILD, MILD)))
    assert contexts[0].dry_streak == 3
    assert not contexts[0].needs_deep_water


def test_every_fallback_day_respects_limits():
    weather = [MILD, RAINY, HOT, MILD, MILD, RAINY, HOT]
    plan = rule_based_plan(plants(), forecast(*weather, past=(MILD, MILD, MILD)), facts=[])
    assert [d.date for d in plan] == [START + dt.timedelta(days=i) for i in range(7)]
    for day in plan:
        assert 1 <= len(day.tasks) <= MAX_TASKS_PER_DAY
        assert all(len(t.action.split()) <= MAX_ACTION_WORDS for t in day.tasks)
        assert all(t.plant in {"Tulsi", "Chillies"} for t in day.tasks)


def test_no_routine_watering_for_plants_already_watered_daily():
    garden = plants()
    for plant in garden:
        plant.notes = "Balcony pot, watered every morning and evening."
    plan = rule_based_plan(garden, forecast(*[MILD] * 7, past=(WET, WET, WET)), facts=[])
    assert not any("Water lightly" in t.action for day in plan for t in day.tasks)


def test_model_watering_dropped_for_plants_on_a_routine_but_rules_still_apply():
    garden = plants()
    garden[0].notes = "Watered every morning and evening."  # Tulsi
    model_day = PlanDay(date=START, tasks=[
        PlanTask(plant="Tulsi", action="Water lightly", reason="Sunny."),
        PlanTask(plant="Chillies", action="Water lightly", reason="Sunny."),
    ])
    context = day_contexts(forecast(MILD, past=(WET, WET, WET)))[0]
    assert actions(enforce_rules(model_day, context, garden)) == ["Water lightly"]
    assert enforce_rules(model_day, context, garden).tasks[0].plant == "Chillies"

    dry = day_contexts(forecast(MILD, past=(MILD, MILD, MILD)))[0]
    assert "Water deeply at the roots" in actions(enforce_rules(PlanDay(date=START), dry, garden))


def test_fallback_can_plan_a_subset_of_dates():
    plan = rule_based_plan(plants(), forecast(MILD, MILD, MILD), facts=[], dates=[START + dt.timedelta(days=1)])
    assert [d.date for d in plan] == [START + dt.timedelta(days=1)]


def test_recent_pests_come_first():
    facts = [{"date": "2026-10-03", "plant": "chilli plant", "health_flags": ["aphids on new leaves"]}]
    day = rule_based_plan(plants(), forecast(HOT), facts=facts)[0]
    assert day.tasks[0] == PlanTask(plant="Chillies", action="Spray neem oil on leaves",
                                    reason="Pests reported in your check-in.")


def test_old_health_flags_are_ignored():
    facts = [{"date": "2026-09-20", "plant": "Chillies", "health_flags": ["wilting"]}]
    assert health_tasks(facts, plants(), START) == []


def test_enforce_removes_watering_on_rainy_day():
    model_day = PlanDay(date=START, tasks=[
        PlanTask(plant="Chillies", action="Water the chillies", reason="Dry soil"),
        PlanTask(plant="Tulsi", action="Pinch the flower spikes", reason="Keeps it leafy"),
    ])
    context = day_contexts(forecast(RAINY))[0]
    fixed = enforce_rules(model_day, context, plants())
    assert actions(fixed) == ["Skip watering today", "Pinch the flower spikes"]


def test_enforce_keeps_model_task_that_already_covers_the_rule():
    model_day = PlanDay(date=START, tasks=[
        PlanTask(plant="Chillies", action="Shade the chilli pot", reason="Very hot"),
    ])
    fixed = enforce_rules(model_day, day_contexts(forecast(HOT))[0], plants())
    assert actions(fixed) == ["Shade the chilli pot"]


def test_match_plant_handles_loose_names():
    garden = plants()
    assert match_plant("chillies", garden).name == "Chillies"
    assert match_plant("Chilli plant", garden).name == "Chillies"
    assert match_plant("tulsi", garden).name == "Tulsi"
    assert match_plant("rose", garden) is None
    assert match_plant("  ", garden) is None


def test_serious_issue_detection():
    assert serious_issues(["leaves wilting", "root rot"]) == ["wilting", "rot"]
    assert serious_issues(["new flowers"]) == []


def test_is_watering():
    assert is_watering("Water deeply at the roots")
    assert not is_watering("Skip watering today")
    assert not is_watering("Cut back on watering")
