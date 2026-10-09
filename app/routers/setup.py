from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import qrcode
import qrcode.image.svg
from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_session
from app.deps import get_llm, require_setup_access
from app.models import Garden, LocationType, Plant
from app.scheduler import plan_now, schedule_garden
from app.services.llm import LLM
from app.services.plans import current_week_plan, garden_today
from app.services.weather import WeatherError, search_places
from app.templating import templates

router = APIRouter(prefix="/setup", dependencies=[Depends(require_setup_access)])

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1"}


def today_url(garden: Garden) -> str:
    return f"{get_settings().public_base_url.rstrip('/')}/today/{garden.checkin_token}"


def qr_svg(url: str) -> str:
    image = qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=2)
    return image.to_string(encoding="unicode")


async def current_garden(session: AsyncSession) -> Garden | None:
    """Tendril is self-hosted for one gardener, so there is one garden."""
    return await session.scalar(select(Garden).order_by(Garden.id).limit(1))


async def _require_garden(session: AsyncSession) -> Garden:
    garden = await current_garden(session)
    if garden is None:
        raise HTTPException(status_code=409, detail="Choose the garden's city first.")
    return garden


def _back(anchor: str, query: str = "") -> RedirectResponse:
    return RedirectResponse(f"/setup{query}#{anchor}", status_code=303)


@router.get("", response_class=HTMLResponse)
async def setup_page(
    request: Request,
    q: str = "",
    name: str = "",
    planning: bool = False,
    session: AsyncSession = Depends(get_session),
):
    garden = await current_garden(session)
    places, search_error = [], None
    if q.strip():
        try:
            places = await search_places(q)
        except WeatherError:
            search_error = "City search is unavailable right now. Please try again in a moment."

    settings = get_settings()
    context = {
        "garden": garden,
        "plants": garden.plants if garden else [],
        "q": q,
        "name": name,
        "places": places,
        "search_error": search_error,
        "location_types": list(LocationType),
        "planning": planning,
        "morning_hour": settings.morning_hour,
        "base_url": settings.public_base_url,
        "phone_reachable": (urlparse(settings.public_base_url).hostname or "") not in _LOCAL_HOSTS,
    }
    if garden:
        url = today_url(garden)
        plan = await current_week_plan(session, garden, garden_today(garden))
        context |= {
            "today_url": url,
            "lockscreen_url": f"{url}/lockscreen.png",
            "qr": qr_svg(url),
            "has_plan": plan is not None,
            "plan_source": plan.source if plan else None,
            "model": settings.ollama_model,
        }
    return templates.TemplateResponse(request, "setup.html", context)


@router.post("/garden")
async def save_garden(
    request: Request,
    lat: float = Form(..., ge=-90, le=90),
    lon: float = Form(..., ge=-180, le=180),
    timezone: str = Form(..., max_length=64),
    place: str = Form("", max_length=200),
    name: str = Form("", max_length=100),
    session: AsyncSession = Depends(get_session),
):
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="Unknown timezone") from exc

    name = name.strip() or (f"{place.split(',')[0].strip()} garden" if place.strip() else "My garden")
    garden = await current_garden(session)
    if garden is None:
        garden = Garden(name=name, lat=lat, lon=lon, timezone=timezone)
        session.add(garden)
    else:
        garden.name, garden.lat, garden.lon, garden.timezone = name, lat, lon, timezone
    await session.commit()

    if request.app.state.scheduler:
        schedule_garden(request.app.state.scheduler, garden)
    return _back("plants")


@router.post("/plants")
async def add_plant(
    name: str = Form(..., min_length=1, max_length=60),
    location_type: LocationType = Form(...),
    species: str = Form("", max_length=200),
    notes: str = Form("", max_length=500),
    session: AsyncSession = Depends(get_session),
):
    garden = await _require_garden(session)
    session.add(
        Plant(
            garden_id=garden.id,
            name=name.strip(),
            species=species.strip(),
            location_type=location_type,
            notes=notes.strip(),
        )
    )
    await session.commit()
    return _back("plants")


@router.post("/plants/{plant_id}/delete")
async def remove_plant(plant_id: int, session: AsyncSession = Depends(get_session)):
    garden = await _require_garden(session)
    plant = await session.get(Plant, plant_id)
    if plant is not None and plant.garden_id == garden.id:
        await session.delete(plant)
        await session.commit()
    return _back("plants")


@router.post("/plan")
async def plan_this_week(
    background: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
    llm: LLM = Depends(get_llm),
):
    garden = await _require_garden(session)
    background.add_task(plan_now, garden.id, llm)
    return _back("phone", "?planning=1")
