import datetime as dt

from app.schemas import DAY_SCHEMA, WEEK_SCHEMA, PlanTask
from app.services.llm import LLMError
from app.services.planner import adjust_today, build_week_prompt, plan_week
from app.services.rules import day_contexts
from tests.factories import HOT, MILD, RAINY, START, WET, FakeLLM, forecast, plants

D0, D1, D2 = (START + dt.timedelta(days=i) for i in range(3))


def task(plant="Chillies", action="Pinch off side shoots", reason="Bushier growth."):
    return {"plant": plant, "action": action, "reason": reason}


def week(*days_tasks):
    return {"days": [{"date": d.isoformat(), "tasks": tasks} for d, tasks in zip((D0, D1, D2), days_tasks)]}


GOOD_WEEK = week([task("chillies")], [task("Tulsi", "Harvest top leaves", "Encourages new growth.")], [])
MILD_3 = forecast(MILD, MILD, MILD, past=(WET, WET, WET))


async def test_valid_model_plan_is_used():
    llm = FakeLLM(GOOD_WEEK)
    result = await plan_week(plants(), MILD_3, facts=[], llm=llm)

    assert result.source == "model"
    assert [d.date for d in result.days] == [D0, D1, D2]
    assert result.days[0].tasks[0].plant == "Chillies"  # normalised from "chillies"
    assert result.days[1].tasks[0].action == "Harvest top leaves"
    assert len(result.days[2].tasks) == 1  # an empty day gets one routine task
    messages, schema = llm.calls[0]
    assert schema == WEEK_SCHEMA
    prompt = messages[-1]["content"]
    assert "Chillies (Capsicum annuum), pot" in prompt
    assert D2.isoformat() in prompt


async def test_invalid_json_is_retried_once_with_feedback():
    llm = FakeLLM("not json at all", GOOD_WEEK)
    result = await plan_week(plants(), MILD_3, facts=[], llm=llm)

    assert result.source == "model"
    assert len(llm.calls) == 2
    retry_messages = llm.calls[1][0]
    assert retry_messages[-2] == {"role": "assistant", "content": "not json at all"}
    assert "invalid" in retry_messages[-1]["content"]


async def test_two_bad_replies_fall_back_to_rules():
    llm = FakeLLM("{}", "still wrong")
    result = await plan_week(plants(), MILD_3, facts=[], llm=llm)

    assert result.source == "rules"
    assert len(llm.calls) == 2
    assert [d.date for d in result.days] == [D0, D1, D2]
    assert all(d.tasks for d in result.days)


async def test_model_down_falls_back_to_rules():
    llm = FakeLLM(LLMError("connection refused"), LLMError("connection refused"))
    result = await plan_week(plants(), MILD_3, facts=[], llm=llm)
    assert result.source == "rules"


async def test_too_many_tasks_is_invalid():
    three = [task(), task("Tulsi"), task(action="Check soil moisture")]
    llm = FakeLLM(week(three, [], []), GOOD_WEEK)
    result = await plan_week(plants(), MILD_3, facts=[], llm=llm)
    assert result.source == "model"
    assert len(llm.calls) == 2


async def test_long_action_is_invalid():
    long_action = task(action="Water the chilli plants slowly in the evening")
    llm = FakeLLM(week([long_action], [], []), GOOD_WEEK)
    await plan_week(plants(), MILD_3, facts=[], llm=llm)
    assert "under 6 words" in llm.calls[1][0][-1]["content"]


async def test_unknown_plant_is_invalid_and_names_real_plants():
    llm = FakeLLM(week([task("Rose")], [], []), GOOD_WEEK)
    await plan_week(plants(), MILD_3, facts=[], llm=llm)
    feedback = llm.calls[1][0][-1]["content"]
    assert "unknown plant 'Rose'" in feedback
    assert "Tulsi, Chillies" in feedback


async def test_missing_dates_are_invalid():
    llm = FakeLLM({"days": [{"date": D0.isoformat(), "tasks": []}]}, GOOD_WEEK)
    await plan_week(plants(), MILD_3, facts=[], llm=llm)
    assert "missing dates" in llm.calls[1][0][-1]["content"]


async def test_model_watering_on_rainy_day_is_overridden():
    rainy = forecast(RAINY, MILD, MILD, past=(WET, WET, WET))
    llm = FakeLLM(week([task(action="Water the chillies")], [], []))
    result = await plan_week(plants(), rainy, facts=[], llm=llm)

    assert result.source == "model"
    assert [t.action for t in result.days[0].tasks] == ["Skip watering today"]
    assert "Needed: skip watering today for Chillies (85% chance of rain from 4 PM)." in llm.calls[0][0][-1]["content"]


async def test_reasons_copied_from_the_prompt_are_dropped():
    copied = task(action="Pinch off side shoots", reason="At most 2 tasks per day; fewer is fine.")
    llm = FakeLLM(week([copied], [], []))
    result = await plan_week(plants(), MILD_3, facts=[], llm=llm)
    assert result.days[0].tasks[0].reason == "Partly cloudy day, up to 30°C."


async def test_rule_tasks_take_the_rule_reason():
    dry = forecast(MILD, MILD, MILD, past=(MILD, MILD, MILD))
    llm = FakeLLM(week([task(action="Water deeply at the roots", reason="Needed: water deeply")], [], []))
    result = await plan_week(plants(), dry, facts=[], llm=llm)
    assert result.days[0].tasks[0].reason == "3 dry days in a row."


def test_prompt_shows_an_example_with_a_real_plant_name():
    prompt = build_week_prompt(plants(), day_contexts(MILD_3), [])
    assert 'Example of the format for one day:\n{"date": "2026-10-04", "tasks": [{"plant": "Tulsi"' in prompt
    assert "compact JSON" in prompt


async def test_copied_reasons_become_weather_notes():
    same = "Warm days bring aphids."
    llm = FakeLLM(week(
        [task("Tulsi", "Check under leaves for pests", same)],
        [task("Chillies", "Pinch off side shoots", same)],
        [task("Tulsi", "Harvest top leaves", "")],
    ))
    result = await plan_week(plants(), MILD_3, facts=[], llm=llm)
    reasons = [day.tasks[0].reason for day in result.days]
    assert reasons == [same, "Partly cloudy day, up to 30°C.", "Partly cloudy day, up to 30°C."]


async def test_rule_tasks_on_the_wrong_day_are_dropped_and_repeats_limited():
    deep = task("Tulsi", "Water deeply at the roots", "Copied.")
    pinch = task("Chillies", "Pinch off side shoots", "Bushier.")
    llm = FakeLLM(week([deep, pinch], [deep, pinch], [deep, pinch]))
    result = await plan_week(plants(), MILD_3, facts=[], llm=llm)  # not dry, so no deep watering

    actions = [[t.action for t in day.tasks] for day in result.days]
    assert actions[0] == ["Pinch off side shoots"]
    assert actions[1] == ["Pinch off side shoots"]
    assert len(actions[2]) == 1 and actions[2][0] != "Pinch off side shoots"  # third repeat replaced


async def test_check_in_facts_reach_the_prompt_and_the_plan():
    facts = [{"date": "2026-10-03", "plant": "Chillies", "done": ["watered"], "observations": ["curled leaves"],
              "health_flags": ["aphids"]}]
    llm = FakeLLM(GOOD_WEEK)
    result = await plan_week(plants(), MILD_3, facts=facts, llm=llm)

    prompt = llm.calls[0][0][-1]["content"]
    assert "2026-10-03, Chillies; done: watered; observed: curled leaves; problems: aphids" in prompt
    assert result.days[0].tasks[0].action == "Spray neem oil on leaves"


async def test_plan_only_selected_dates():
    llm = FakeLLM({"days": [{"date": D2.isoformat(), "tasks": [task()]}]})
    result = await plan_week(plants(), MILD_3, facts=[], llm=llm, dates=[D2])
    assert [d.date for d in result.days] == [D2]
    assert D0.isoformat() not in llm.calls[0][0][-1]["content"]


async def test_no_plants_means_empty_days_without_calling_the_model():
    llm = FakeLLM()
    result = await plan_week([], MILD_3, facts=[], llm=llm)
    assert all(d.tasks == [] for d in result.days)
    assert llm.calls == []


async def test_adjust_today_uses_model_and_rules():
    hot = forecast(HOT, MILD, past=(WET, WET, WET))
    current = [PlanTask(plant="Chillies", action="Water lightly in the morning", reason="Mild day.")]
    reply = {"date": "wrong-date", "tasks": [task(action="Water early, before 8 AM", reason="Heat later.")]}
    llm = FakeLLM(reply)

    result = await adjust_today(plants(), hot, D0, current, ["heat spike"], facts=[], llm=llm)

    assert result.source == "model"
    day = result.days[0]
    assert day.date == D0
    assert [t.action for t in day.tasks] == ["Move pot into afternoon shade", "Water early, before 8 AM"]
    messages, schema = llm.calls[0]
    assert schema == DAY_SCHEMA
    assert "heat spike" in messages[-1]["content"]
    assert "Water lightly in the morning" in messages[-1]["content"]


async def test_adjust_today_falls_back_to_rules():
    rainy = forecast(RAINY, MILD, past=(WET, WET, WET))
    llm = FakeLLM("nope", "nope")
    result = await adjust_today(plants(), rainy, D0, [], ["rain added"], facts=[], llm=llm)
    assert result.source == "rules"
    assert result.days[0].tasks[0].action == "Skip watering today"
