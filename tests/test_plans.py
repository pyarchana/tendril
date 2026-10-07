import datetime as dt

from sqlalchemy import func, select

from app.models import CheckIn, Task, TaskStatus, WeekPlan
from app.services.plans import create_week_plan, recent_facts, refresh_today, replan_rest_of_week
from scripts.seed import seed
from tests.factories import MILD, RAINY, START, WET, FakeLLM, forecast

TODAY = START
D1, D2 = TODAY + dt.timedelta(days=1), TODAY + dt.timedelta(days=2)
MILD_3 = forecast(MILD, MILD, MILD, past=(WET, WET, WET))


def week_reply(*days_tasks, start=TODAY):
    return {
        "days": [
            {"date": (start + dt.timedelta(days=i)).isoformat(), "tasks": tasks}
            for i, tasks in enumerate(days_tasks)
        ]
    }


def t(plant, action, reason="Because."):
    return {"plant": plant, "action": action, "reason": reason}


GOOD = week_reply(
    [t("Chillies", "Check soil moisture"), t("Tulsi", "Harvest top leaves")],
    [t("Tulsi", "Pinch flower spikes")],
    [],
)


async def tasks_on(session, date) -> list[tuple[str, str, TaskStatus]]:
    rows = await session.scalars(select(Task).where(Task.date == date).order_by(Task.id))
    return [(task.plant.name, task.action, task.status) for task in rows]


async def test_create_week_plan_stores_plan_and_tasks(session):
    garden = await seed(session)
    plan = await create_week_plan(session, garden, today=TODAY, forecast=MILD_3, llm=FakeLLM(GOOD))

    assert plan.week_start == TODAY
    assert plan.source == "model"
    assert [d["date"] for d in plan.days] == [TODAY.isoformat(), D1.isoformat(), D2.isoformat()]
    assert plan.forecast["days"][0]["date"] == TODAY.isoformat()
    assert await tasks_on(session, TODAY) == [
        ("Chillies", "Check soil moisture", TaskStatus.pending),
        ("Tulsi", "Harvest top leaves", TaskStatus.pending),
    ]
    assert len(await tasks_on(session, D2)) == 1  # empty model day topped up with a routine task


async def test_rerunning_replaces_pending_tasks_and_keeps_done_ones(session):
    garden = await seed(session)
    await create_week_plan(session, garden, today=TODAY, forecast=MILD_3, llm=FakeLLM(GOOD))
    done = await session.scalar(select(Task).where(Task.action == "Check soil moisture"))
    done.status = TaskStatus.done
    await session.commit()

    await create_week_plan(session, garden, today=TODAY, forecast=MILD_3, llm=FakeLLM(GOOD))

    assert await session.scalar(select(func.count()).select_from(WeekPlan)) == 1
    assert await tasks_on(session, TODAY) == [
        ("Chillies", "Check soil moisture", TaskStatus.done),
        ("Tulsi", "Harvest top leaves", TaskStatus.pending),
    ]


async def test_model_failure_still_produces_a_plan(session):
    garden = await seed(session)
    plan = await create_week_plan(session, garden, today=TODAY, forecast=MILD_3, llm=FakeLLM("bad", "bad"))
    assert plan.source == "rules"
    assert len(await tasks_on(session, TODAY)) == 2


async def test_recent_facts_cover_two_weeks(session):
    garden = await seed(session)
    session.add_all([
        CheckIn(garden_id=garden.id, date=TODAY - dt.timedelta(days=20), facts={"plant": "Tulsi", "done": ["old"]}),
        CheckIn(garden_id=garden.id, date=TODAY - dt.timedelta(days=2), facts={"plant": "Tulsi", "done": ["watered"]}),
        CheckIn(garden_id=garden.id, date=TODAY, facts={}),
    ])
    await session.commit()

    facts = await recent_facts(session, garden, TODAY)

    assert facts == [{"date": (TODAY - dt.timedelta(days=2)).isoformat(), "plant": "Tulsi", "done": ["watered"]}]


async def test_refresh_without_meaningful_change_does_nothing(session):
    garden = await seed(session)
    await create_week_plan(session, garden, today=TODAY, forecast=MILD_3, llm=FakeLLM(GOOD))
    llm = FakeLLM()

    changes = await refresh_today(session, garden, today=TODAY, forecast=MILD_3, llm=llm)

    assert changes == []
    assert llm.calls == []


async def test_refresh_adjusts_only_today_when_rain_arrives(session):
    garden = await seed(session)
    plan = await create_week_plan(session, garden, today=TODAY, forecast=MILD_3, llm=FakeLLM(GOOD))
    done = await session.scalar(select(Task).where(Task.action == "Harvest top leaves"))
    done.status = TaskStatus.done
    await session.commit()

    rainy_now = forecast(RAINY, MILD, MILD, past=(WET, WET, WET))
    llm = FakeLLM({"date": TODAY.isoformat(), "tasks": [t("Chillies", "Skip watering today", "Rain at 4 PM.")]})
    changes = await refresh_today(session, garden, today=TODAY, forecast=rainy_now, llm=llm)

    assert changes == ["rain added"]
    assert await tasks_on(session, TODAY) == [
        ("Tulsi", "Harvest top leaves", TaskStatus.done),
        ("Chillies", "Skip watering today", TaskStatus.pending),
    ]
    assert await tasks_on(session, D1) == [("Tulsi", "Pinch flower spikes", TaskStatus.pending)]
    await session.refresh(plan)
    assert plan.days[0]["tasks"][0]["action"] == "Skip watering today"
    assert plan.days[1]["tasks"][0]["action"] == "Pinch flower spikes"
    assert plan.forecast["days"][0]["rain_probability"] == RAINY["rain_probability"]


async def test_refresh_without_a_plan_creates_one(session):
    garden = await seed(session)
    changes = await refresh_today(session, garden, today=TODAY, forecast=MILD_3, llm=FakeLLM(GOOD))
    assert changes == ["new plan"]
    assert await session.scalar(select(func.count()).select_from(WeekPlan)) == 1


async def test_replan_rest_of_week_keeps_earlier_days(session):
    garden = await seed(session)
    await create_week_plan(session, garden, today=TODAY, forecast=MILD_3, llm=FakeLLM(GOOD))
    session.add(CheckIn(garden_id=garden.id, date=D1, facts={"plant": "Chillies", "health_flags": ["aphids"]}))
    await session.commit()

    later = forecast(MILD, MILD, past=(WET, WET, WET), start=D1)
    llm = FakeLLM(week_reply([t("Tulsi", "Harvest top leaves")], [], start=D1))
    plan = await replan_rest_of_week(session, garden, today=D1, forecast=later, llm=llm)

    assert [d["date"] for d in plan.days] == [TODAY.isoformat(), D1.isoformat(), D2.isoformat()]
    assert plan.days[0]["tasks"][0]["action"] == "Check soil moisture"  # yesterday untouched
    assert [task[1] for task in await tasks_on(session, D1)] == ["Spray neem oil on leaves", "Harvest top leaves"]
    assert "problems: aphids" in llm.calls[0][0][-1]["content"]
