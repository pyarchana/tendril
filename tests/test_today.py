import datetime as dt
import io
from zoneinfo import ZoneInfo

import pytest
from PIL import Image, ImageColor
from sqlalchemy import select

from app.deps import get_clock
from app.models import Task, TaskStatus
from app.schemas import PlanTask
from app.services.render import BG, LOCKSCREEN_HEIGHT, LOCKSCREEN_TOP_PAD, WIDTH
from app.services.plans import create_week_plan
from app.services.today import ensure_plan_image, load_today, summary_line
from scripts.seed import seed
from tests.factories import HOT, MILD, RAINY, START, WET, FakeLLM, day, forecast

TZ = ZoneInfo("Asia/Kolkata")
D1 = START + dt.timedelta(days=1)

PLAN = {
    "days": [
        {"date": START.isoformat(), "tasks": [
            {"plant": "Chillies", "action": "Skip watering today", "reason": "Rain from 4 PM."},
            {"plant": "Tulsi", "action": "Harvest top leaves", "reason": "Keeps it bushy."},
        ]},
        {"date": D1.isoformat(), "tasks": [{"plant": "Tulsi", "action": "Check soil moisture", "reason": "Dry."}]},
        {"date": (START + dt.timedelta(days=2)).isoformat(), "tasks": []},
    ]
}


def at(hour: int, minute: int = 0) -> dt.datetime:
    return dt.datetime.combine(START, dt.time(hour, minute), tzinfo=TZ)


@pytest.fixture
def clock(app):
    current = {"now": at(9)}
    app.dependency_overrides[get_clock] = lambda: (lambda garden: current["now"])
    return current


async def planned_garden(session):
    garden = await seed(session)
    weather = forecast(RAINY, MILD, MILD, past=(WET, WET, WET))
    await create_week_plan(session, garden, today=START, forecast=weather, llm=FakeLLM(PLAN))
    return garden


async def task_statuses(session, date) -> list[TaskStatus]:
    """Re-read task rows the API changed through its own session."""
    query = select(Task).where(Task.date == date).order_by(Task.id).execution_options(populate_existing=True)
    return [t.status for t in await session.scalars(query)]


async def test_unknown_token_is_404(api, clock):
    for response in (
        await api.get("/today/nope"),
        await api.get("/today/nope/plan.png"),
        await api.post("/t/nope/done"),
    ):
        assert response.status_code == 404


async def test_page_before_first_plan(api, session, clock):
    garden = await seed(session)
    page = await api.get(f"/today/{garden.checkin_token}")
    assert page.status_code == 200
    assert "Your first plan is on its way" in page.text
    assert (await api.get(f"/today/{garden.checkin_token}/plan.png")).status_code == 404


async def test_page_shows_todays_plan(api, session, clock):
    garden = await planned_garden(session)
    token = garden.checkin_token

    page = await api.get(f"/today/{token}")

    assert page.status_code == 200
    assert "Chillies: skip watering today. Rain at 4 PM." in page.text
    assert "Harvest top leaves" in page.text
    assert f'action="/t/{token}/done"' in page.text
    assert f'action="/t/{token}/skip"' in page.text
    assert f'href="/c/{token}"' in page.text
    assert f"/today/{token}/plan.png?v=" in page.text
    assert "Still time" not in page.text


async def test_plan_image_is_rendered_once_and_cached(api, session, clock, isolated_settings):
    garden = await planned_garden(session)

    first = await api.get(f"/today/{garden.checkin_token}/plan.png")
    second = await api.get(f"/today/{garden.checkin_token}/plan.png")

    assert first.status_code == 200
    assert first.headers["content-type"] == "image/png"
    assert Image.open(io.BytesIO(first.content)).size == (1080, 1920)
    assert second.content == first.content
    assert len(list(isolated_settings.images_dir.glob("*.png"))) == 1


async def test_image_is_rerendered_when_the_plan_changes(session, clock, isolated_settings):
    garden = await planned_garden(session)
    old_path = await ensure_plan_image(await load_today(session, garden, at(9)))

    task = await session.scalar(select(Task).where(Task.action == "Harvest top leaves"))
    task.action = "Pinch flower spikes"
    await session.commit()
    new_path = await ensure_plan_image(await load_today(session, garden, at(9)))

    assert new_path != old_path
    assert not old_path.exists()
    assert new_path.exists()


async def test_done_button_marks_todays_tasks_and_redirects(api, session, clock):
    garden = await planned_garden(session)
    token = garden.checkin_token

    response = await api.post(f"/t/{token}/done", headers={"accept": "text/html"})

    assert response.status_code == 303
    assert response.headers["location"] == f"/today/{token}?updated=done"
    assert await task_statuses(session, START) == [TaskStatus.done, TaskStatus.done]
    assert await task_statuses(session, D1) == [TaskStatus.pending]
    done = await session.scalar(select(Task).where(Task.date == START))
    assert done.completed_at is not None

    page = await api.get(response.headers["location"])
    assert "Marked done. Nice work." in page.text
    assert f'action="/t/{token}/done"' not in page.text


async def test_skip_works_as_a_json_api(api, session, clock):
    garden = await planned_garden(session)

    response = await api.post(f"/t/{garden.checkin_token}/skip")

    assert response.json() == {"status": "skipped", "updated": 2}
    assert await task_statuses(session, START) == [TaskStatus.skipped, TaskStatus.skipped]
    again = await api.post(f"/t/{garden.checkin_token}/skip")
    assert again.json()["updated"] == 0


async def test_evening_nudge_only_after_six_with_pending_tasks(api, session, clock):
    garden = await planned_garden(session)
    url = f"/today/{garden.checkin_token}"

    clock["now"] = at(17, 59)
    assert "Still time" not in (await api.get(url)).text
    clock["now"] = at(18, 30)
    assert "Still time for today's tasks" in (await api.get(url)).text
    await api.post(f"/t/{garden.checkin_token}/done")
    assert "Still time" not in (await api.get(url)).text


async def test_manifest_starts_on_the_today_page(api, session, clock):
    garden = await seed(session)
    response = await api.get(f"/today/{garden.checkin_token}/manifest.webmanifest")
    assert response.headers["content-type"].startswith("application/manifest+json")
    assert response.json()["start_url"] == f"/today/{garden.checkin_token}"


def test_summary_line_variants():
    cards = [PlanTask(plant="Chillies", action="Move pot into afternoon shade", reason="")]
    assert summary_line(cards, day(START, **HOT)) == "Chillies: move pot into afternoon shade. Up to 38°."
    assert summary_line([], day(START)) == "Nothing needed in the garden today."
    assert summary_line([], None) == "Nothing needed in the garden today."


async def test_lockscreen_image_is_phone_shaped_and_never_cached(api, session, clock):
    garden = await planned_garden(session)
    assert (await api.get(f"/today/{garden.checkin_token}/lockscreen.png")).status_code == 200

    response = await api.get(f"/today/{garden.checkin_token}/lockscreen.png")

    assert response.headers["cache-control"] == "no-store"
    image = Image.open(io.BytesIO(response.content)).convert("RGB")
    assert image.size == (WIDTH, LOCKSCREEN_HEIGHT) == (1080, 2340)
    bg = ImageColor.getrgb(BG)
    top = image.crop((0, 0, WIDTH, LOCKSCREEN_TOP_PAD))
    bottom = image.crop((0, LOCKSCREEN_TOP_PAD + 1920, WIDTH, LOCKSCREEN_HEIGHT))
    assert top.getcolors() == [(WIDTH * LOCKSCREEN_TOP_PAD, bg)]
    assert bottom.getcolors() == [(WIDTH * (LOCKSCREEN_HEIGHT - LOCKSCREEN_TOP_PAD - 1920), bg)]


async def test_only_versioned_plan_links_are_cached(api, session, clock):
    garden = await planned_garden(session)
    plain = await api.get(f"/today/{garden.checkin_token}/plan.png")
    versioned = await api.get(f"/today/{garden.checkin_token}/plan.png?v=abc")
    assert plain.headers["cache-control"] == "no-store"
    assert versioned.headers["cache-control"] == "private, max-age=86400"


async def test_lockscreen_needs_a_plan(api, session, clock):
    garden = await seed(session)
    assert (await api.get(f"/today/{garden.checkin_token}/lockscreen.png")).status_code == 404


async def test_same_lockscreen_link_follows_the_plan(api, session, clock, isolated_settings):
    """What a phone automation sees: one fixed link, fetched each morning."""
    garden = await planned_garden(session)
    url = f"/today/{garden.checkin_token}/lockscreen.png"
    today = (await api.get(url)).content

    task = await session.scalar(select(Task).where(Task.action == "Harvest top leaves"))
    task.action = "Pinch flower spikes"
    await session.commit()
    replanned = (await api.get(url)).content

    clock["now"] = dt.datetime.combine(D1, dt.time(7, 5), tzinfo=TZ)
    next_morning = (await api.get(url)).content

    assert len({today, replanned, next_morning}) == 3
    lock_files = sorted(p.name for p in isolated_settings.images_dir.glob("lock-*.png"))
    assert [name.split("-")[2:5] for name in lock_files] == [START.isoformat().split("-"), D1.isoformat().split("-")]


async def test_page_says_when_the_backup_rules_made_the_plan(api, session, clock):
    garden = await seed(session)
    weather = forecast(RAINY, MILD, MILD, past=(WET, WET, WET))
    await create_week_plan(session, garden, today=START, forecast=weather, llm=FakeLLM("bad", "bad"))

    page = await api.get(f"/today/{garden.checkin_token}")

    assert "this plan comes from Tendril's backup rules" in page.text


async def test_no_backup_note_when_the_model_made_the_plan(api, session, clock):
    garden = await planned_garden(session)
    page = await api.get(f"/today/{garden.checkin_token}")
    assert "backup rules" not in page.text


async def test_image_key_is_long_enough_to_never_collide(session, clock):
    garden = await planned_garden(session)
    view = await load_today(session, garden, at(9))
    assert len(view.image_key) == 16
