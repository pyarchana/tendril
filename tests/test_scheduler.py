import datetime as dt
from zoneinfo import ZoneInfo

import httpx
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models import WeekPlan
from app.scheduler import create_scheduler, job_id, morning_job, sync_jobs
from app.services.plans import create_week_plan
from app.services.weather import FORECAST_URL
from scripts.seed import seed
from tests.factories import MILD, RAINY, START, WET, FakeLLM, forecast, open_meteo_payload

TZ = ZoneInfo("Asia/Kolkata")
MONDAY = START + dt.timedelta(days=1)
WEATHER = forecast(MILD, MILD, MILD, past=(WET, WET, WET))


def at(date: dt.date, hour: int = 7) -> dt.datetime:
    return dt.datetime.combine(date, dt.time(hour), tzinfo=TZ)


def mock_forecast(weather=WEATHER):
    respx.get(FORECAST_URL).mock(return_value=httpx.Response(200, json=open_meteo_payload(weather)))


async def test_each_garden_gets_a_7am_job_in_its_own_timezone(session):
    garden = await seed(session)
    scheduler = create_scheduler()

    assert await sync_jobs(scheduler) == 1

    job = scheduler.get_job(job_id(garden.id))
    assert job.args == (garden.id,)
    next_run = job.trigger.get_next_fire_time(None, at(START, hour=9))
    assert next_run == at(START + dt.timedelta(days=1), hour=7)
    assert next_run.utcoffset() == dt.timedelta(hours=5, minutes=30)


@respx.mock
async def test_sunday_morning_makes_the_week_plan_and_image(session, isolated_settings):
    garden = await seed(session)
    mock_forecast()

    outcome = await morning_job(garden.id, now=at(START), llm=FakeLLM("bad", "bad"))

    assert START.weekday() == 6
    assert outcome == "week plan"
    assert (await session.scalar(select(WeekPlan))).week_start == START
    assert len(list(isolated_settings.images_dir.glob("*.png"))) == 1


@respx.mock
async def test_weekday_morning_refreshes_the_weather(session):
    garden = await seed(session)
    await create_week_plan(session, garden, today=START, forecast=WEATHER, llm=FakeLLM("bad", "bad"))
    mock_forecast()

    assert await morning_job(garden.id, now=at(MONDAY), llm=FakeLLM()) == "refreshed (no change)"


@respx.mock
async def test_weekday_morning_adjusts_today_when_rain_arrives(session):
    garden = await seed(session)
    await create_week_plan(session, garden, today=START, forecast=WEATHER, llm=FakeLLM("bad", "bad"))
    mock_forecast(forecast(MILD, RAINY, MILD, past=(WET, WET, WET)))

    outcome = await morning_job(garden.id, now=at(MONDAY), llm=FakeLLM("bad", "bad"))

    assert outcome == "refreshed (rain added)"


@respx.mock
async def test_weekday_without_a_plan_makes_one(session):
    garden = await seed(session)
    mock_forecast()
    assert await morning_job(garden.id, now=at(MONDAY), llm=FakeLLM("bad", "bad")) == "week plan"


@respx.mock
async def test_morning_job_survives_a_weather_outage(session):
    garden = await seed(session)
    respx.get(FORECAST_URL).mock(return_value=httpx.Response(503))
    assert await morning_job(garden.id, now=at(START)) == "failed"


async def test_unknown_garden_is_ignored(session):
    assert await morning_job(999) == "no garden"


def test_app_starts_and_stops_the_scheduler(monkeypatch):
    monkeypatch.setenv("SCHEDULER_ENABLED", "true")
    from app.config import get_settings
    from app.main import create_app

    get_settings.cache_clear()
    app = create_app()
    with TestClient(app):
        assert app.state.scheduler.running
    assert not app.state.scheduler.running
