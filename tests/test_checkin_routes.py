import datetime as dt
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx
from sqlalchemy import select

from app.deps import get_clock, get_llm, get_transcriber
from app.models import CheckIn, Task, TaskStatus
from app.routers.checkin import MAX_AUDIO_BYTES
from app.services.plans import create_week_plan
from app.services.speech import SpeechError
from app.services.weather import FORECAST_URL
from scripts.seed import seed
from tests.factories import MILD, START, WET, FakeLLM, forecast, open_meteo_payload

NOW = dt.datetime.combine(START, dt.time(19, 0), tzinfo=ZoneInfo("Asia/Kolkata"))
D1, D2 = START + dt.timedelta(days=1), START + dt.timedelta(days=2)
WEATHER = forecast(MILD, MILD, MILD, past=(WET, WET, WET))
PLAN = {
    "days": [
        {"date": START.isoformat(), "tasks": [
            {"plant": "Chillies", "action": "Water lightly in the morning", "reason": "Warm day."},
            {"plant": "Tulsi", "action": "Check soil moisture", "reason": "Routine."},
        ]},
        {"date": D1.isoformat(), "tasks": []},
        {"date": D2.isoformat(), "tasks": []},
    ]
}
WATERED = {"plant": "Chillies", "done": ["watered"], "observations": [], "health_flags": []}
APHIDS = {"plant": "Chillies", "done": [], "observations": [], "health_flags": ["aphids"]}
WEBM = ("note.webm", b"\x1aE\xdf\xa3 fake opus audio", "audio/webm;codecs=opus")


class FakeTranscriber:
    def __init__(self, text: str = "", error: Exception | None = None):
        self.text, self.error, self.calls = text, error, []

    async def transcribe(self, path, hint=""):
        self.calls.append((path, hint))
        if self.error:
            raise self.error
        return self.text


@pytest.fixture
def fakes(app):
    """Swap the clock, model and transcriber for test doubles. Tests fill them in."""
    state = {"llm": FakeLLM(), "transcriber": FakeTranscriber()}
    app.dependency_overrides[get_clock] = lambda: (lambda garden: NOW)
    app.dependency_overrides[get_llm] = lambda: state["llm"]
    app.dependency_overrides[get_transcriber] = lambda: state["transcriber"]
    return state


async def planned_garden(session):
    garden = await seed(session)
    await create_week_plan(session, garden, today=START, forecast=WEATHER, llm=FakeLLM(PLAN))
    return garden


async def test_check_in_page(api, session, fakes):
    garden = await seed(session)
    page = await api.get(f"/c/{garden.checkin_token}")
    assert page.status_code == 200
    assert 'id="record"' in page.text
    assert "Hold to record" in page.text
    assert "0:00 / 0:30" in page.text
    assert "Type instead" in page.text
    assert (await api.get("/c/nope")).status_code == 404


async def test_voice_note_is_transcribed_and_ticks_off_a_task(api, session, fakes, isolated_settings):
    garden = await planned_garden(session)
    fakes["transcriber"] = FakeTranscriber("I watered the chillies.")
    fakes["llm"] = FakeLLM(WATERED)

    response = await api.post(f"/c/{garden.checkin_token}/audio", files={"audio": WEBM})

    assert response.status_code == 200
    body = response.json()
    assert body["message"] == "Marked “Water lightly in the morning” done."
    assert body["transcript"] == "I watered the chillies."
    assert body["marked"] == ["Water lightly in the morning"]
    assert body["plan_changed"] is False
    path, hint = fakes["transcriber"].calls[0]
    assert path.suffix == ".webm" and path.exists()
    assert path.parent == isolated_settings.audio_dir
    assert hint == "Garden check-in about Chillies, Tulsi."
    check_in = await session.scalar(select(CheckIn))
    assert check_in.audio_path == str(path)
    task = await session.scalar(
        select(Task).where(Task.action == "Water lightly in the morning").execution_options(populate_existing=True)
    )
    assert task.status == TaskStatus.done


@pytest.mark.parametrize(
    ("upload", "status"),
    [
        (("note.txt", b"hello", "text/plain"), 415),
        (("note.webm", b"x" * (MAX_AUDIO_BYTES + 1), "audio/webm"), 413),
        (("note.webm", b"", "audio/webm"), 422),
    ],
)
async def test_bad_uploads_are_rejected(api, session, fakes, upload, status):
    garden = await seed(session)
    response = await api.post(f"/c/{garden.checkin_token}/audio", files={"audio": upload})
    assert response.status_code == status
    assert fakes["transcriber"].calls == []


async def test_silence_asks_to_try_again(api, session, fakes):
    garden = await seed(session)
    fakes["transcriber"] = FakeTranscriber("   ")
    response = await api.post(f"/c/{garden.checkin_token}/audio", files={"audio": WEBM})
    assert response.status_code == 422
    assert "Didn't catch any words" in response.json()["detail"]


async def test_transcription_failure_is_503(api, session, fakes):
    garden = await seed(session)
    fakes["transcriber"] = FakeTranscriber(error=SpeechError("model download failed"))
    response = await api.post(f"/c/{garden.checkin_token}/audio", files={"audio": WEBM})
    assert response.status_code == 503
    assert await session.scalar(select(CheckIn)) is None


async def test_typed_note(api, session, fakes):
    garden = await planned_garden(session)
    fakes["llm"] = FakeLLM({"plant": "Tulsi", "done": [], "observations": ["new flowers"], "health_flags": []})

    response = await api.post(f"/c/{garden.checkin_token}/text", data={"text": "  Tulsi has new flowers  "})

    assert response.json()["message"] == "Noted: new flowers."
    check_in = await session.scalar(select(CheckIn))
    assert check_in.transcript == "Tulsi has new flowers"
    assert check_in.audio_path is None


@respx.mock
async def test_serious_problem_replans_and_shows_on_today_page(api, session, fakes):
    garden = await planned_garden(session)
    respx.get(FORECAST_URL).mock(return_value=httpx.Response(200, json=open_meteo_payload(WEATHER)))
    fakes["transcriber"] = FakeTranscriber("There are aphids all over the chillies.")
    fakes["llm"] = FakeLLM(APHIDS, "bad plan", "still bad")  # extraction, then a re-plan that falls back to rules

    response = await api.post(f"/c/{garden.checkin_token}/audio", files={"audio": WEBM})

    assert response.json()["plan_changed"] is True
    actions = [t.action for t in await session.scalars(
        select(Task).where(Task.date == START).execution_options(populate_existing=True)
    )]
    assert "Spray neem oil on leaves" in actions
    page = await api.get(f"/today/{garden.checkin_token}")
    assert "Plan changed after your voice note: pests on Chillies." in page.text


async def test_pests_on_an_unknown_plant_ask_instead_of_treating(api, session, fakes):
    garden = await planned_garden(session)
    fakes["llm"] = FakeLLM({"plant": "roses", "done": [], "observations": [], "health_flags": ["aphids"]})

    response = await api.post(f"/c/{garden.checkin_token}/text", data={"text": "Aphids on the roses!"})

    body = response.json()
    assert body["plan_changed"] is False
    assert body["message"] == (
        "Noted pests. Which plant was it? Send a quick note naming it, and Tendril will plan the treatment."
    )
    actions = [t.action for t in await session.scalars(
        select(Task).where(Task.date == START).execution_options(populate_existing=True)
    )]
    assert "Spray neem oil on leaves" not in actions
    page = await api.get(f"/today/{garden.checkin_token}")
    assert "Pests reported, but on which plant? Send a quick note naming it." in page.text
