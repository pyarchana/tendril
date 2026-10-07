import datetime as dt

from app.schemas import DAY_SCHEMA, WEEK_SCHEMA, PlanTask
from app.services.llm import LLMError
from app.services.planner import adjust_today, plan_week
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
    assert result.days[2].tasks == []
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
    assert "Must: skip watering today (Chillies)" in llm.calls[0][0][-1]["content"]


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
