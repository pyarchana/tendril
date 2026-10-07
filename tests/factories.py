"""Small builders shared by tests."""

import datetime as dt
import json

from app.models import LocationType, Plant
from app.services.weather import DayForecast, Forecast

START = dt.date(2026, 10, 4)  # a Sunday

MILD = dict(temp_max=30, temp_min=21, rain_probability=10, rain_mm=0, weather_code=1, condition="partly")
RAINY = dict(temp_max=27, temp_min=22, rain_probability=85, rain_mm=14, weather_code=63, condition="rain",
             rain_start_hour=16)
HOT = dict(temp_max=38, temp_min=27, rain_probability=5, rain_mm=0, weather_code=0, condition="sunny")
WET = dict(MILD, rain_probability=55, rain_mm=6)  # rained, but not enough to skip watering


def day(date: dt.date, **weather) -> DayForecast:
    return DayForecast(date=date, **(MILD | weather))


def forecast(*days: dict, past: tuple[dict, ...] = (WET, WET, WET), start: dt.date = START) -> Forecast:
    """Build a forecast from weather dicts. `past` are the days before `start`."""
    past_days = [day(start - dt.timedelta(days=len(past) - i), **w) for i, w in enumerate(past)]
    upcoming = [day(start + dt.timedelta(days=i), **w) for i, w in enumerate(days)]
    return Forecast(timezone="Asia/Kolkata", past_days=past_days, days=upcoming)


def plants() -> list[Plant]:
    return [
        Plant(id=1, name="Tulsi", species="Ocimum tenuiflorum", location_type=LocationType.terrace, notes=""),
        Plant(id=2, name="Chillies", species="Capsicum annuum", location_type=LocationType.pot, notes=""),
    ]


class FakeLLM:
    """Returns scripted replies in order and records every call."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls: list[tuple[list[dict], dict | None]] = []

    async def chat_json(self, messages, schema=None):
        self.calls.append((messages, schema))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply if isinstance(reply, str) else json.dumps(reply)
