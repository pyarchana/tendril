import datetime as dt

import pytest
from PIL import Image, ImageColor

from app.schemas import PlanDay, PlanTask
from app.services.render import (
    BG,
    CARD,
    CARDS_BOTTOM,
    CARDS_TOP,
    CARD_GAP,
    HEIGHT,
    MARGIN,
    STRIP_TOP,
    TILE_HEIGHT,
    WIDTH,
    _layout_cards,
    body_font,
    render_plan_image,
    save_plan_image,
    short_label,
    weather_pill_text,
    wrap,
)
from tests.factories import RAINY, day

TODAY = dt.date(2026, 10, 7)
CONDITIONS = ["sunny", "partly", "cloudy", "rain", "storm", "fog", "snow", "unknown"]
TWO_TASKS = [
    PlanTask(plant="Chillies", action="Skip watering today", reason="85% chance of rain from 4 PM."),
    PlanTask(plant="Tulsi", action="Harvest the top leaves", reason="Keeps it bushy."),
]


def upcoming(conditions=CONDITIONS[:6]):
    days = []
    for i, condition in enumerate(conditions, start=1):
        date = TODAY + dt.timedelta(days=i)
        plan = PlanDay(date=date, tasks=[PlanTask(plant="Tulsi", action="Water deeply at the roots")])
        days.append((date, day(date, condition=condition), plan))
    return days


def rgb(hex_color: str) -> tuple[int, int, int]:
    return ImageColor.getrgb(hex_color)


def test_image_is_full_hd_portrait_with_empty_top_third():
    image = render_plan_image(TODAY, day(TODAY, **RAINY), TWO_TASKS, upcoming())
    assert image.size == (WIDTH, HEIGHT) == (1080, 1920)
    assert image.mode == "RGB"
    top_third = image.crop((0, 0, WIDTH, HEIGHT // 3))
    assert top_third.getcolors() == [(WIDTH * (HEIGHT // 3), rgb(BG))]


def test_task_cards_and_day_tiles_are_drawn():
    image = render_plan_image(TODAY, day(TODAY), TWO_TASKS, upcoming())
    assert image.getpixel((MARGIN + 12, CARDS_TOP + 120)) == rgb(CARD)
    assert image.getpixel((MARGIN + 6, STRIP_TOP + TILE_HEIGHT // 2)) == rgb(CARD)
    assert image.getpixel((MARGIN - 20, CARDS_TOP + 120)) == rgb(BG)


@pytest.mark.parametrize("tasks", [[], TWO_TASKS[:1], TWO_TASKS])
def test_renders_zero_one_or_two_tasks(tasks):
    image = render_plan_image(TODAY, None, tasks, upcoming())
    assert image.getpixel((MARGIN + 12, CARDS_TOP + 60)) == rgb(CARD)


def test_every_weather_condition_can_be_drawn():
    for condition in CONDITIONS:
        render_plan_image(TODAY, day(TODAY, condition=condition), [], upcoming([condition] * 6))


def test_long_text_still_fits_above_the_strip():
    long = PlanTask(
        plant="Curry leaf (murraya koenigii)",
        action="Move pot into afternoon shade",
        reason=("Heat peaks at 38°C and the leaves scorch quickly in small terracotta pots on a bright balcony " * 3)[:200],
    )
    cards = _layout_cards([long, long])
    assert sum(card.height for card in cards) + CARD_GAP <= CARDS_BOTTOM - CARDS_TOP
    assert all(len(card.action_lines) <= 2 and len(card.reason_lines) <= 2 for card in cards)
    assert cards[0].reason_lines[-1].endswith("…")


def test_prefers_one_line_actions_over_wrapping():
    cards = _layout_cards([PlanTask(plant="Chillies", action="Move pot into afternoon shade", reason="Hot.")] * 2)
    assert all(len(card.action_lines) == 1 for card in cards)


def test_wrap_breaks_lines_and_truncates():
    lines = wrap("word " * 40, body_font(32), 300, 2)
    assert len(lines) == 2
    assert lines[-1].endswith("…")
    assert wrap("short", body_font(32), 300, 2) == ["short"]


@pytest.mark.parametrize(
    ("action", "label"),
    [
        ("Skip watering today", "Skip water"),
        ("Move pot into afternoon shade", "Shade"),
        ("Water deeply at the roots", "Deep water"),
        ("Spray neem oil on leaves", "Neem spray"),
        ("Check soil moisture", "Check soil"),
        ("Look under leaves for pests", "Pest check"),
        ("Stake the tomatoes", "Stake the"),
    ],
)
def test_short_label(action, label):
    assert short_label(PlanDay(date=TODAY, tasks=[PlanTask(plant="Tulsi", action=action)])) == label


def test_short_label_for_empty_and_unplanned_days():
    assert short_label(PlanDay(date=TODAY)) == "Rest"
    assert short_label(None) == "—"


def test_weather_pill_text():
    assert weather_pill_text(day(TODAY, **RAINY)) == "27°  ·  Rain at 4 PM"
    assert weather_pill_text(day(TODAY, temp_max=31.4, rain_probability=40)) == "31°  ·  40% rain"
    assert weather_pill_text(day(TODAY, rain_probability=5)) == "30°  ·  No rain"
    assert weather_pill_text(None) == "No forecast"


def test_save_plan_image_writes_png(tmp_path):
    image = render_plan_image(TODAY, day(TODAY), TWO_TASKS, upcoming())
    path = save_plan_image(image, tmp_path / "images" / "today.png")
    with Image.open(path) as saved:
        assert saved.format == "PNG"
        assert saved.size == (1080, 1920)
