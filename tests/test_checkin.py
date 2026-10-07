import datetime as dt
from zoneinfo import ZoneInfo

import httpx
import respx
from sqlalchemy import select

from app.models import CheckIn, Task, TaskStatus, WeekPlan
from app.schemas import FACTS_SCHEMA
from app.services.checkin import matches_task, record_check_in, replan_after_check_in, rule_based_facts
from app.services.plans import create_week_plan
from app.services.weather import FORECAST_URL
from scripts.seed import seed
from tests.factories import MILD, START, WET, FakeLLM, forecast, open_meteo_payload, plants

NOW = dt.datetime.combine(START, dt.time(19, 0), tzinfo=ZoneInfo("Asia/Kolkata"))
D1, D2 = START + dt.timedelta(days=1), START + dt.timedelta(days=2)
WEATHER = forecast(MILD, MILD, MILD, past=(WET, WET, WET))
PLAN = {
    "days": [
        {"date": START.isoformat(), "tasks": [
            {"plant": "Chillies", "action": "Water deeply at the roots", "reason": "Dry spell."},
            {"plant": "Tulsi", "action": "Check soil moisture", "reason": "Routine."},
        ]},
        {"date": D1.isoformat(), "tasks": [{"plant": "Tulsi", "action": "Harvest top leaves", "reason": "Bushy."}]},
        {"date": D2.isoformat(), "tasks": []},
    ]
}


def facts(plant="", done=(), observations=(), health_flags=()):
    return {"plant": plant, "done": list(done), "observations": list(observations), "health_flags": list(health_flags)}


async def planned_garden(session):
    garden = await seed(session)
    await create_week_plan(session, garden, today=START, forecast=WEATHER, llm=FakeLLM(PLAN))
    return garden


async def statuses(session) -> dict[str, TaskStatus]:
    query = select(Task).where(Task.date == START).execution_options(populate_existing=True)
    return {task.action: task.status for task in await session.scalars(query)}


async def test_voice_note_ticks_off_the_matching_task(session):
    garden = await planned_garden(session)
    llm = FakeLLM(facts("chilli plant", done=["watered"]))

    result = await record_check_in(session, garden, transcript="Watered the chillies.", now=NOW, llm=llm)

    assert result.source == "model"
    assert result.facts.plant == "Chillies"
    assert [task.action for task in result.marked] == ["Water deeply at the roots"]
    assert await statuses(session) == {
        "Water deeply at the roots": TaskStatus.done,
        "Check soil moisture": TaskStatus.pending,
    }
    assert result.message == "Marked “Water deeply at the roots” done."
    stored = await session.scalar(select(CheckIn))
    assert stored.transcript == "Watered the chillies."
    assert stored.facts["done"] == ["watered"]
    messages, schema = llm.calls[0]
    assert schema == FACTS_SCHEMA
    assert "Watered the chillies." in messages[-1]["content"]
    assert "Chillies: Water deeply at the roots" in messages[-1]["content"]


async def test_unknown_plant_becomes_whole_garden(session):
    garden = await planned_garden(session)
    llm = FakeLLM(facts("roses", done=["all done"]))

    result = await record_check_in(session, garden, transcript="All done today.", now=NOW, llm=llm)

    assert result.facts.plant == ""
    assert len(result.marked) == 2


async def test_serious_problem_is_flagged(session):
    garden = await planned_garden(session)
    llm = FakeLLM(facts("Chillies", observations=["curled leaves"], health_flags=["aphids under the leaves"]))

    result = await record_check_in(session, garden, transcript="Aphids on the chillies.", now=NOW, llm=llm)

    assert result.serious == ["pests"]
    assert result.marked == []
    assert result.message == "Noted pests on Chillies, so the rest of the week is being re-planned."


async def test_observation_message(session):
    garden = await planned_garden(session)
    llm = FakeLLM(facts("Tulsi", observations=["new flowers"]))
    result = await record_check_in(session, garden, transcript="Tulsi has flowers.", now=NOW, llm=llm)
    assert result.message == "Noted: new flowers."


async def test_model_failure_uses_keyword_extraction(session):
    garden = await planned_garden(session)
    llm = FakeLLM("nope", "still nope")

    result = await record_check_in(
        session, garden, transcript="I watered the chillies and saw aphids on the leaves", now=NOW, llm=llm
    )

    assert result.source == "rules"
    assert result.facts.plant == "Chillies"
    assert result.facts.done == ["watered"]
    assert result.serious == ["pests"]
    assert [task.action for task in result.marked] == ["Water deeply at the roots"]


def test_keyword_extraction_respects_negation_and_yellow_leaves():
    found = rule_based_facts("I didn't water the tulsi, leaves look yellow", plants())
    assert found.plant == "Tulsi"
    assert found.done == []
    assert found.health_flags == ["yellow leaves"]


def test_keyword_extraction_puts_the_problem_on_the_right_plant():
    found = rule_based_facts("Watered the tulsi. Found aphids under the chilli leaves.", plants())
    assert found.plant == "Chillies"
    assert found.health_flags == ["pests"]
    reversed_order = rule_based_facts("The chillies look great and the tulsi is wilting", plants())
    assert reversed_order.plant == "Tulsi"
    assert rule_based_facts("Watered the tulsi and the chillies", plants()).plant == ""


def test_keyword_extraction_keeps_plain_notes_as_observations():
    found = rule_based_facts("Lovely morning on the terrace", plants())
    assert found.observations == ["Lovely morning on the terrace"]


def test_watering_does_not_tick_off_skip_watering():
    skip = Task(action="Skip watering today")
    water = Task(action="Water deeply at the roots")
    assert not matches_task(skip, ["watered"])
    assert matches_task(skip, ["skipped watering"])
    assert matches_task(water, ["watered the roots"])
    assert not matches_task(water, ["harvested leaves"])


@respx.mock
async def test_background_replan_rewrites_the_rest_of_the_week(session):
    garden = await planned_garden(session)
    session.add(CheckIn(garden_id=garden.id, date=START, facts=facts("Chillies", health_flags=["aphids"])))
    await session.commit()
    respx.get(FORECAST_URL).mock(return_value=httpx.Response(200, json=open_meteo_payload(WEATHER)))
    replanned = {"days": [
        {"date": d.isoformat(), "tasks": [{"plant": "Chillies", "action": "Check leaves for aphids", "reason": "Pests."}]}
        for d in (START, D1, D2)
    ]}

    await replan_after_check_in(garden.id, START, llm=FakeLLM(replanned))

    plan = await session.scalar(select(WeekPlan).execution_options(populate_existing=True))
    assert plan.days[0]["tasks"][0]["action"] == "Spray neem oil on leaves"  # rules put treatment first
    assert plan.days[1]["tasks"][0]["action"] == "Check leaves for aphids"
