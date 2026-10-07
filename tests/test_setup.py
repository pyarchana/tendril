import httpx
import respx
from sqlalchemy import func, select

from app.config import get_settings
from app.deps import get_llm
from app.models import Garden, Plant, WeekPlan
from app.services.plans import garden_today
from app.services.weather import FORECAST_URL, GEOCODING_URL
from scripts.seed import seed
from tests.factories import MILD, WET, FakeLLM, forecast, open_meteo_payload

PUNE = {
    "name": "Pune", "latitude": 18.52, "longitude": 73.86,
    "country": "India", "admin1": "Maharashtra", "timezone": "Asia/Kolkata",
}
PUNE_FORM = {"lat": "18.52", "lon": "73.86", "timezone": "Asia/Kolkata", "place": "Pune, Maharashtra, India"}


async def test_root_redirects_to_setup(api, session):
    response = await api.get("/")
    assert response.status_code == 307
    assert response.headers["location"] == "/setup"


async def test_empty_setup_page(api, session):
    page = await api.get("/setup")
    assert page.status_code == 200
    assert "Where is your garden?" in page.text
    assert "Choose your city first." in page.text


@respx.mock
async def test_city_search_lists_places(api, session):
    respx.get(GEOCODING_URL).mock(return_value=httpx.Response(200, json={"results": [PUNE]}))
    page = await api.get("/setup", params={"q": "Pune", "name": "Terrace"})
    assert "Pune, Maharashtra, India" in page.text
    assert 'name="lat" value="18.52"' in page.text
    assert 'name="name" value="Terrace"' in page.text


@respx.mock
async def test_city_search_failure_is_friendly(api, session):
    respx.get(GEOCODING_URL).mock(side_effect=httpx.ConnectTimeout("offline"))
    page = await api.get("/setup", params={"q": "Pune"})
    assert page.status_code == 200
    assert "City search is unavailable right now" in page.text


async def test_choosing_a_place_creates_then_updates_the_one_garden(api, session):
    response = await api.post("/setup/garden", data=PUNE_FORM)
    assert response.status_code == 303
    assert response.headers["location"] == "/setup#plants"
    garden = await session.scalar(select(Garden))
    assert (garden.name, garden.lat, garden.timezone) == ("Pune garden", 18.52, "Asia/Kolkata")
    token = garden.checkin_token

    await api.post("/setup/garden", data={**PUNE_FORM, "lat": "28.61", "lon": "77.21", "name": "Terrace"})

    assert await session.scalar(select(func.count()).select_from(Garden)) == 1
    garden = await session.scalar(select(Garden).execution_options(populate_existing=True))
    assert (garden.name, garden.lat) == ("Terrace", 28.61)
    assert garden.checkin_token == token  # the phone link keeps working


async def test_bad_location_is_rejected(api, session):
    assert (await api.post("/setup/garden", data={**PUNE_FORM, "timezone": "Mars/Olympus"})).status_code == 422
    assert (await api.post("/setup/garden", data={**PUNE_FORM, "lat": "120"})).status_code == 422


async def test_add_and_remove_plants(api, session):
    await seed(session)
    response = await api.post(
        "/setup/plants", data={"name": " Mint ", "location_type": "window", "notes": "Kitchen sill"}
    )
    assert response.headers["location"] == "/setup#plants"
    mint = await session.scalar(select(Plant).where(Plant.name == "Mint"))
    assert (mint.location_type, mint.notes) == ("window", "Kitchen sill")
    assert "Kitchen sill" in (await api.get("/setup")).text

    await api.post(f"/setup/plants/{mint.id}/delete")
    session.expunge_all()
    assert [p.name for p in await session.scalars(select(Plant).order_by(Plant.id))] == ["Chillies", "Tulsi"]


async def test_plant_validation(api, session):
    await seed(session)
    assert (await api.post("/setup/plants", data={"name": "Rose", "location_type": "roof"})).status_code == 422
    assert (await api.post("/setup/plants", data={"name": "", "location_type": "pot"})).status_code == 422


async def test_plants_need_a_garden_first(api, session):
    response = await api.post("/setup/plants", data={"name": "Mint", "location_type": "pot"})
    assert response.status_code == 409


async def test_phone_section_shows_qr_and_private_link(api, session, monkeypatch):
    garden = await seed(session)
    page = await api.get("/setup")
    assert "<svg" in page.text
    assert f"http://localhost:8000/today/{garden.checkin_token}" in page.text
    assert "which your phone can't open" in page.text

    monkeypatch.setenv("PUBLIC_BASE_URL", "https://garden.example.com/")
    get_settings.cache_clear()
    page = await api.get("/setup")
    assert f"https://garden.example.com/today/{garden.checkin_token}" in page.text
    assert "which your phone can't open" not in page.text


async def test_setup_password(api, session, monkeypatch):
    garden = await seed(session)
    monkeypatch.setenv("SETUP_PASSWORD", "s3cret")
    get_settings.cache_clear()

    locked = await api.get("/setup")
    assert locked.status_code == 401
    assert locked.headers["www-authenticate"].startswith("Basic")
    assert (await api.get("/setup", auth=("admin", "wrong"))).status_code == 401
    assert (await api.post("/setup/plants", data={"name": "Mint", "location_type": "pot"})).status_code == 401
    assert (await api.get("/setup", auth=("anyone", "s3cret"))).status_code == 200
    # The gardener's own pages use the private link, not the setup password.
    assert (await api.get(f"/c/{garden.checkin_token}")).status_code == 200


@respx.mock
async def test_plan_now_runs_in_the_background(api, app, session, isolated_settings):
    garden = await seed(session)
    weather = forecast(MILD, MILD, MILD, past=(WET, WET, WET), start=garden_today(garden))
    respx.get(FORECAST_URL).mock(return_value=httpx.Response(200, json=open_meteo_payload(weather)))
    app.dependency_overrides[get_llm] = lambda: FakeLLM("bad", "bad")

    response = await api.post("/setup/plan")

    assert response.headers["location"] == "/setup?planning=1#phone"
    plan = await session.scalar(select(WeekPlan))
    assert plan.source == "rules"
    assert list(isolated_settings.images_dir.glob("*.png"))
    page = await api.get(response.headers["location"])
    assert "Planning the week now" in page.text
    assert "Re-plan this week now" in page.text
