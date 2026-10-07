"""Open-Meteo forecast and geocoding (free, no API key)."""

import datetime as dt
import logging

import httpx
from pydantic import BaseModel

from app.config import get_settings

log = logging.getLogger(__name__)

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"

# Thresholds shared with the planner rules.
RAIN_SKIP_PROBABILITY = 60  # % above which watering is skipped
HEAT_TEMP_C = 35.0  # max temperature above which plants need shade
HEAT_JUMP_C = 4.0  # rise in max temperature that counts as a heat spike
RAIN_HOUR_PROBABILITY = 50  # % an hour must reach to count as "rain at"
RAIN_ICON_PROBABILITY = 30  # % below which a rainy weather code is drawn as cloud
DRY_DAY_MM = 1.0  # less rain than this (and low probability) is a dry day


class WeatherError(Exception):
    """Open-Meteo could not be reached or returned something unusable."""


class DayForecast(BaseModel):
    date: dt.date
    temp_max: float
    temp_min: float
    rain_probability: int
    rain_mm: float
    weather_code: int
    condition: str
    rain_start_hour: int | None = None

    @property
    def is_dry(self) -> bool:
        return self.rain_mm < DRY_DAY_MM and self.rain_probability < RAIN_HOUR_PROBABILITY

    @property
    def is_rainy(self) -> bool:
        return self.rain_probability > RAIN_SKIP_PROBABILITY

    @property
    def is_hot(self) -> bool:
        return self.temp_max > HEAT_TEMP_C


class Forecast(BaseModel):
    timezone: str
    past_days: list[DayForecast] = []
    days: list[DayForecast]

    @property
    def today(self) -> DayForecast:
        return self.days[0]

    def day(self, date: dt.date) -> DayForecast | None:
        return next((d for d in self.days if d.date == date), None)


class Place(BaseModel):
    name: str
    country: str = ""
    admin1: str = ""
    lat: float
    lon: float
    timezone: str

    @property
    def label(self) -> str:
        return ", ".join(part for part in (self.name, self.admin1, self.country) if part)


def condition_for(code: int) -> str:
    """Map a WMO weather code to one of the icon names the renderer draws."""
    if code == 0:
        return "sunny"
    if code in (1, 2):
        return "partly"
    if code == 3:
        return "cloudy"
    if code in (45, 48):
        return "fog"
    if 71 <= code <= 77 or code in (85, 86):
        return "snow"
    if code >= 95:
        return "storm"
    if 51 <= code <= 67 or 80 <= code <= 82:
        return "rain"
    return "cloudy"


def format_hour(hour: int) -> str:
    """16 -> '4 PM'."""
    return f"{hour % 12 or 12} {'AM' if hour < 12 else 'PM'}"


def detect_change(planned: DayForecast, current: DayForecast) -> list[str]:
    """Describe meaningful differences between the planned and current forecast for a day."""
    changes = []
    if current.is_rainy and not planned.is_rainy:
        changes.append("rain added")
    if planned.is_rainy and not current.is_rainy:
        changes.append("rain removed")
    if (current.is_hot and not planned.is_hot) or current.temp_max - planned.temp_max >= HEAT_JUMP_C:
        changes.append("heat spike")
    return changes


def _num(value, default: float = 0.0) -> float:
    return default if value is None else float(value)


def _rain_start_hours(hourly: dict) -> dict[dt.date, int]:
    """First local hour each day where rain probability reaches the threshold."""
    starts: dict[dt.date, int] = {}
    times = hourly.get("time", [])
    probabilities = hourly.get("precipitation_probability", [])
    for stamp, probability in zip(times, probabilities):
        if probability is None or probability < RAIN_HOUR_PROBABILITY:
            continue
        moment = dt.datetime.fromisoformat(stamp)
        starts.setdefault(moment.date(), moment.hour)
    return starts


def parse_forecast(payload: dict, past_days: int) -> Forecast:
    try:
        daily = payload["daily"]
        rain_starts = _rain_start_hours(payload.get("hourly", {}))
        parsed = []
        for i, day in enumerate(daily["time"]):
            date = dt.date.fromisoformat(day)
            code = int(_num(daily["weather_code"][i]))
            forecast_day = DayForecast(
                date=date,
                temp_max=round(_num(daily["temperature_2m_max"][i]), 1),
                temp_min=round(_num(daily["temperature_2m_min"][i]), 1),
                rain_probability=int(_num(daily["precipitation_probability_max"][i])),
                rain_mm=round(_num(daily["precipitation_sum"][i]), 1),
                weather_code=code,
                condition=condition_for(code),
                rain_start_hour=rain_starts.get(date),
            )
            # The daily code is the worst moment of the day; a passing drizzle on an
            # otherwise dry or unlikely-rain day would show a misleading rain icon.
            if forecast_day.condition == "rain" and (
                forecast_day.is_dry or forecast_day.rain_probability < RAIN_ICON_PROBABILITY
            ):
                forecast_day.condition = "cloudy"
            parsed.append(forecast_day)
        return Forecast(timezone=payload.get("timezone", "UTC"), past_days=parsed[:past_days], days=parsed[past_days:])
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise WeatherError(f"Unexpected forecast payload: {exc}") from exc


async def _get_json(url: str, params: dict) -> dict:
    timeout = get_settings().weather_timeout
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            return response.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("Open-Meteo request failed: %s", exc)
        raise WeatherError(str(exc)) from exc


async def fetch_forecast(lat: float, lon: float, timezone: str, days: int = 7, past_days: int = 3) -> Forecast:
    """Daily forecast in the garden's local time, plus a few past days for the dry-spell rule."""
    params = {
        "latitude": lat,
        "longitude": lon,
        "timezone": timezone,
        "forecast_days": days,
        "past_days": past_days,
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,precipitation_sum,weather_code",
        "hourly": "precipitation_probability",
    }
    payload = await _get_json(FORECAST_URL, params)
    return parse_forecast(payload, past_days)


async def search_places(query: str, count: int = 5) -> list[Place]:
    query = query.strip()
    if len(query) < 2:
        return []
    payload = await _get_json(GEOCODING_URL, {"name": query, "count": count, "language": "en", "format": "json"})
    places = []
    for result in payload.get("results", []):
        try:
            places.append(
                Place(
                    name=result["name"],
                    country=result.get("country", ""),
                    admin1=result.get("admin1", ""),
                    lat=result["latitude"],
                    lon=result["longitude"],
                    timezone=result.get("timezone", "UTC"),
                )
            )
        except KeyError:
            continue
    return places
