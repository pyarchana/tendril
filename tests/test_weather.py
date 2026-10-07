import datetime as dt

import httpx
import pytest
import respx

from app.services.weather import (
    FORECAST_URL,
    GEOCODING_URL,
    DayForecast,
    WeatherError,
    condition_for,
    detect_change,
    fetch_forecast,
    search_places,
)


def forecast_payload(start: dt.date, rows: list[tuple], hourly_rain: dict[str, int] | None = None) -> dict:
    """Build an Open-Meteo response. Each row: (t_max, t_min, rain_prob, rain_mm, code)."""
    dates = [(start + dt.timedelta(days=i)).isoformat() for i in range(len(rows))]
    hours = [f"{d}T{h:02d}:00" for d in dates for h in range(24)]
    hourly_rain = hourly_rain or {}
    return {
        "timezone": "Asia/Kolkata",
        "daily": {
            "time": dates,
            "temperature_2m_max": [r[0] for r in rows],
            "temperature_2m_min": [r[1] for r in rows],
            "precipitation_probability_max": [r[2] for r in rows],
            "precipitation_sum": [r[3] for r in rows],
            "weather_code": [r[4] for r in rows],
        },
        "hourly": {"time": hours, "precipitation_probability": [hourly_rain.get(h, 0) for h in hours]},
    }


def day(**overrides) -> DayForecast:
    values = dict(
        date=dt.date(2026, 10, 7), temp_max=30, temp_min=22, rain_probability=10,
        rain_mm=0, weather_code=0, condition="sunny",
    )
    return DayForecast(**(values | overrides))


@respx.mock
async def test_fetch_forecast_splits_past_and_upcoming_days():
    start = dt.date(2026, 10, 4)
    rows = [(31, 22, None, 0, 0)] * 3 + [(33, 24, 80, 12.4, 63)] + [(30, 21, 5, 0, 2)] * 6
    payload = forecast_payload(start, rows, hourly_rain={"2026-10-07T16:00": 70, "2026-10-07T18:00": 90})
    route = respx.get(FORECAST_URL).mock(return_value=httpx.Response(200, json=payload))

    forecast = await fetch_forecast(28.61, 77.21, "Asia/Kolkata")

    params = route.calls.last.request.url.params
    assert params["timezone"] == "Asia/Kolkata"
    assert params["forecast_days"] == "7"
    assert params["past_days"] == "3"
    assert len(forecast.past_days) == 3
    assert len(forecast.days) == 7
    assert forecast.past_days[0].rain_probability == 0  # null from the API becomes 0
    today = forecast.today
    assert today.date == dt.date(2026, 10, 7)
    assert today.condition == "rain"
    assert today.rain_start_hour == 16
    assert today.is_rainy
    assert forecast.days[1].rain_start_hour is None
    assert forecast.day(dt.date(2026, 10, 8)).condition == "partly"


@respx.mock
async def test_unlikely_rain_shows_as_cloudy():
    rows = [(34, 25, 6, 0.4, 51), (31, 23, 17, 1.2, 61), (30, 22, 70, 8.0, 61)]
    respx.get(FORECAST_URL).mock(return_value=httpx.Response(200, json=forecast_payload(dt.date(2026, 10, 7), rows)))

    forecast = await fetch_forecast(0, 0, "UTC", days=3, past_days=0)

    assert [d.condition for d in forecast.days] == ["cloudy", "cloudy", "rain"]


@respx.mock
async def test_fetch_forecast_timeout_raises_weather_error():
    respx.get(FORECAST_URL).mock(side_effect=httpx.ConnectTimeout("too slow"))
    with pytest.raises(WeatherError):
        await fetch_forecast(0, 0, "UTC")


@respx.mock
async def test_fetch_forecast_server_error_raises_weather_error():
    respx.get(FORECAST_URL).mock(return_value=httpx.Response(500))
    with pytest.raises(WeatherError):
        await fetch_forecast(0, 0, "UTC")


@respx.mock
async def test_fetch_forecast_bad_payload_raises_weather_error():
    respx.get(FORECAST_URL).mock(return_value=httpx.Response(200, json={"unexpected": True}))
    with pytest.raises(WeatherError):
        await fetch_forecast(0, 0, "UTC")


@respx.mock
async def test_search_places_returns_labelled_results():
    results = {
        "results": [
            {"name": "Pune", "latitude": 18.52, "longitude": 73.86, "country": "India",
             "admin1": "Maharashtra", "timezone": "Asia/Kolkata"},
            {"name": "Broken"},  # missing coordinates is skipped
        ]
    }
    route = respx.get(GEOCODING_URL).mock(return_value=httpx.Response(200, json=results))

    places = await search_places("Pune")

    assert route.calls.last.request.url.params["name"] == "Pune"
    assert len(places) == 1
    assert places[0].label == "Pune, Maharashtra, India"
    assert places[0].timezone == "Asia/Kolkata"


@respx.mock
async def test_search_places_with_no_matches():
    respx.get(GEOCODING_URL).mock(return_value=httpx.Response(200, json={"generationtime_ms": 0.1}))
    assert await search_places("Nowhereville") == []


async def test_search_places_ignores_too_short_queries():
    assert await search_places(" a ") == []


@pytest.mark.parametrize(
    ("code", "condition"),
    [(0, "sunny"), (2, "partly"), (3, "cloudy"), (45, "fog"), (53, "rain"),
     (81, "rain"), (73, "snow"), (95, "storm")],
)
def test_condition_for_maps_wmo_codes(code, condition):
    assert condition_for(code) == condition


def test_detect_change_rain_added():
    assert detect_change(day(rain_probability=20), day(rain_probability=75)) == ["rain added"]


def test_detect_change_rain_removed():
    assert detect_change(day(rain_probability=80), day(rain_probability=30)) == ["rain removed"]


def test_detect_change_heat_spike_over_threshold():
    assert detect_change(day(temp_max=33), day(temp_max=36)) == ["heat spike"]


def test_detect_change_heat_spike_by_jump():
    assert detect_change(day(temp_max=26), day(temp_max=30.5)) == ["heat spike"]


def test_detect_change_ignores_small_shifts():
    assert detect_change(day(rain_probability=20, temp_max=30), day(rain_probability=45, temp_max=32)) == []


def test_dry_day_flags():
    assert day(rain_mm=0.2, rain_probability=20).is_dry
    assert not day(rain_mm=4, rain_probability=20).is_dry
    assert not day(rain_mm=0, rain_probability=70).is_dry
