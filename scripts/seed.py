"""Create a demo garden with two plants.

Usage: python -m scripts.seed [--reset]
"""

import argparse
import asyncio

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import dispose_engine, get_sessionmaker, init_db
from app.models import Garden, LocationType, Plant

DEMO_GARDEN = {
    "name": "Demo Garden",
    "lat": 28.6139,
    "lon": 77.2090,
    "timezone": "Asia/Kolkata",
}

DEMO_PLANTS = [
    {
        "name": "Chillies",
        "species": "Capsicum annuum",
        "location_type": LocationType.pot,
        "notes": "Two pots on the south-facing balcony. Fruiting.",
    },
    {
        "name": "Tulsi",
        "species": "Ocimum tenuiflorum",
        "location_type": LocationType.terrace,
        "notes": "Large terrace planter, full sun.",
    },
]


async def seed(session: AsyncSession, reset: bool = False) -> Garden:
    """Return the demo garden, creating it if there is no garden yet."""
    if reset:
        await session.execute(delete(Garden))
        await session.commit()

    existing = await session.scalar(select(Garden).limit(1))
    if existing is not None:
        return existing

    garden = Garden(**DEMO_GARDEN, plants=[Plant(**plant) for plant in DEMO_PLANTS])
    session.add(garden)
    await session.commit()
    return garden


async def main(reset: bool) -> None:
    settings = get_settings()
    settings.ensure_dirs()
    await init_db()
    async with get_sessionmaker()() as session:
        garden = await seed(session, reset=reset)
        base = settings.public_base_url.rstrip("/")
        print(f"Garden: {garden.name} ({garden.lat}, {garden.lon}, {garden.timezone})")
        for plant in garden.plants:
            print(f"  - {plant.name} [{plant.location_type}]")
        print(f"Check-in token: {garden.checkin_token}")
        print(f"Today page:     {base}/today/{garden.checkin_token}")
    await dispose_engine()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Seed a demo garden.")
    parser.add_argument("--reset", action="store_true", help="delete existing gardens first")
    asyncio.run(main(parser.parse_args().reset))
