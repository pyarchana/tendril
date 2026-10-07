from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.deps import Clock, garden_from_token, get_clock
from app.models import Garden
from app.services.render import BG
from app.services.today import ensure_plan_image, load_today
from app.templating import templates

router = APIRouter()


@router.get("/today/{token}", response_class=HTMLResponse)
async def today_page(
    request: Request,
    updated: str | None = None,
    garden: Garden = Depends(garden_from_token),
    session: AsyncSession = Depends(get_session),
    clock: Clock = Depends(get_clock),
):
    view = await load_today(session, garden, clock(garden))
    return templates.TemplateResponse(
        request,
        "today.html",
        {"view": view, "token": garden.checkin_token, "updated": updated if updated in ("done", "skipped") else None},
    )


@router.get("/today/{token}/plan.png")
async def plan_image(
    garden: Garden = Depends(garden_from_token),
    session: AsyncSession = Depends(get_session),
    clock: Clock = Depends(get_clock),
):
    view = await load_today(session, garden, clock(garden))
    if not view.has_plan:
        raise HTTPException(status_code=404, detail="No plan yet")
    path = await ensure_plan_image(view)
    # URLs carry ?v=<content key>, so a cached copy is never stale.
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "private, max-age=86400"})


@router.get("/today/{token}/manifest.webmanifest")
async def manifest(garden: Garden = Depends(garden_from_token)):
    """Lets the gardener add the Today page to their home screen."""
    return JSONResponse(
        {
            "name": "Tendril",
            "short_name": "Tendril",
            "start_url": f"/today/{garden.checkin_token}",
            "display": "standalone",
            "background_color": BG,
            "theme_color": BG,
            "icons": [{"src": "/static/icon.svg", "sizes": "any", "type": "image/svg+xml"}],
        },
        media_type="application/manifest+json",
    )
