import datetime as dt

from sqlalchemy import func, inspect, select

from app.db import get_engine
from app.models import CheckIn, Garden, LocationType, Plant, Task, TaskStatus, WeekPlan
from scripts.seed import seed


async def make_garden(session) -> Garden:
    garden = Garden(
        name="Balcony",
        lat=19.07,
        lon=72.88,
        timezone="Asia/Kolkata",
        plants=[Plant(name="Basil", species="Ocimum basilicum", location_type=LocationType.window)],
    )
    session.add(garden)
    await session.commit()
    return garden


async def test_garden_gets_unique_random_token(session):
    first = await make_garden(session)
    second = await make_garden(session)
    assert len(first.checkin_token) >= 20
    assert first.checkin_token != second.checkin_token


async def test_task_defaults_and_plant_link(session):
    garden = await make_garden(session)
    task = Task(plant_id=garden.plants[0].id, date=dt.date(2026, 10, 7), action="Water deeply")
    session.add(task)
    await session.commit()

    loaded = await session.scalar(select(Task).where(Task.id == task.id))
    assert loaded.status == TaskStatus.pending
    assert loaded.completed_at is None
    assert loaded.plant.name == "Basil"


async def test_json_columns_round_trip(session):
    garden = await make_garden(session)
    days = [{"date": "2026-10-07", "tasks": [{"plant": "Basil", "action": "Water", "reason": "Dry"}]}]
    session.add(WeekPlan(garden_id=garden.id, week_start=dt.date(2026, 10, 4), forecast={"daily": []}, days=days))
    facts = {"plant": "Basil", "done": ["watered"], "observations": [], "health_flags": ["pests"]}
    session.add(CheckIn(garden_id=garden.id, date=dt.date(2026, 10, 7), transcript="Watered basil", facts=facts))
    await session.commit()
    session.expunge_all()

    plan = await session.scalar(select(WeekPlan))
    check_in = await session.scalar(select(CheckIn))
    assert plan.days == days
    assert plan.source == "model"
    assert check_in.facts == facts


async def test_deleting_garden_cascades(session):
    garden = await make_garden(session)
    session.add(Task(plant_id=garden.plants[0].id, date=dt.date(2026, 10, 7), action="Water"))
    await session.commit()

    await session.delete(garden)
    await session.commit()
    assert await session.scalar(select(func.count()).select_from(Plant)) == 0
    assert await session.scalar(select(func.count()).select_from(Task)) == 0


async def test_seed_creates_two_plants_and_is_idempotent(session):
    garden = await seed(session)
    again = await seed(session)
    assert again.id == garden.id
    assert [p.name for p in garden.plants] == ["Chillies", "Tulsi"]
    assert await session.scalar(select(func.count()).select_from(Garden)) == 1


async def test_seed_reset_replaces_garden(session):
    first = await seed(session)
    second = await seed(session, reset=True)
    assert second.checkin_token != first.checkin_token
    assert await session.scalar(select(func.count()).select_from(Plant)) == 2


async def test_init_db_creates_all_tables(session):
    async with get_engine().connect() as conn:
        tables = await conn.run_sync(lambda sync_conn: inspect(sync_conn).get_table_names())
    assert set(tables) == {"gardens", "plants", "week_plans", "tasks", "check_ins"}
