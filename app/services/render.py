"""Render the day's plan as a 1080x1920 lock-screen-style image.

Everything is drawn at 2x and scaled down, so shapes get smooth edges.
Coordinates in this module are in final (1x) pixels; the Canvas does the scaling.
"""

import datetime as dt
import math
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from app.schemas import PlanDay, PlanTask
from app.services.weather import DayForecast, format_hour

FONTS_DIR = Path(__file__).resolve().parent.parent / "static" / "fonts"

WIDTH, HEIGHT = 1080, 1920
SCALE = 2
MARGIN = 72

BG = "#14231A"
CARD = "#1E3125"
TEXT = "#EEF3E8"
ACCENT = "#C8E07A"
MUTED = "#B9C9B8"
RAIN = "#9CC9E6"

# Vertical layout. The top third stays empty for the phone's clock.
HEADER_Y = 700
PILL_Y = 800
CARDS_TOP = 868
CARDS_BOTTOM = 1516
STRIP_LABEL_Y = 1556
STRIP_TOP = 1584
TILE_HEIGHT = 196
FOOTER_Y = 1852

CARD_PAD = 40
CARD_GAP = 24
ACTION_SIZES = (64, 58, 52, 46)


@lru_cache(maxsize=32)
def heading_font(size: int, weight: int = 700) -> ImageFont.FreeTypeFont:
    font = ImageFont.truetype(str(FONTS_DIR / "BricolageGrotesque.ttf"), size * SCALE)
    optical_size = min(96, max(12, round(size * 0.75)))
    font.set_variation_by_axes([optical_size, weight, 100])
    return font


@lru_cache(maxsize=32)
def body_font(size: int, weight: int = 400) -> ImageFont.FreeTypeFont:
    font = ImageFont.truetype(str(FONTS_DIR / "Figtree.ttf"), size * SCALE)
    font.set_variation_by_axes([weight])
    return font


class Canvas:
    def __init__(self) -> None:
        self.image = Image.new("RGB", (WIDTH * SCALE, HEIGHT * SCALE), BG)
        self.draw = ImageDraw.Draw(self.image)

    @staticmethod
    def _s(values: Sequence[float]) -> list[float]:
        return [v * SCALE for v in values]

    def rounded(self, box: Sequence[float], radius: float, fill: str) -> None:
        self.draw.rounded_rectangle(self._s(box), radius=radius * SCALE, fill=fill)

    def circle(self, cx: float, cy: float, r: float, fill: str) -> None:
        self.draw.ellipse(self._s((cx - r, cy - r, cx + r, cy + r)), fill=fill)

    def line(self, points: Sequence[tuple[float, float]], width: float, fill: str) -> None:
        """A line with round caps."""
        self.draw.line(self._s([v for p in points for v in p]), fill=fill, width=round(width * SCALE))
        for x, y in (points[0], points[-1]):
            self.circle(x, y, width / 2, fill)

    def polygon(self, points: Sequence[tuple[float, float]], fill: str) -> None:
        self.draw.polygon(self._s([v for p in points for v in p]), fill=fill)

    def text(self, xy: tuple[float, float], text: str, font, fill: str, anchor: str = "la") -> None:
        self.draw.text(self._s(xy), text, font=font, fill=fill, anchor=anchor)

    def tracked(self, xy: tuple[float, float], text: str, font, fill: str, tracking: float) -> None:
        """Text with extra letter spacing (for small uppercase labels)."""
        x, y = xy
        for char in text:
            self.text((x, y), char, font, fill)
            x += width_of(char, font) + tracking

    def finish(self) -> Image.Image:
        return self.image.resize((WIDTH, HEIGHT), Image.Resampling.LANCZOS)


def width_of(text: str, font) -> float:
    return font.getlength(text) / SCALE


def wrap(text: str, font, max_width: float, max_lines: int) -> list[str]:
    """Greedy word wrap; the last allowed line is cut with an ellipsis if needed."""
    lines: list[str] = []
    current = ""
    for word in text.split():
        candidate = f"{current} {word}".strip()
        if width_of(candidate, font) <= max_width or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    if len(lines) > max_lines:
        last = " ".join(lines[max_lines - 1 :])
        while last and width_of(last + "…", font) > max_width:
            last = last[:-1]
        lines = [*lines[: max_lines - 1], last.rstrip() + "…"]
    return lines


# --- icons -------------------------------------------------------------------


def leaf(c: Canvas, cx: float, cy: float, size: float, color: str = ACCENT) -> None:
    half_length, half_width = size / 2, size / 4.4
    angle = math.radians(-45)
    outline = []
    for i in range(41):
        t = math.pi * i / 40
        outline.append((-half_length * math.cos(t), -half_width * math.sin(t)))
    for i in range(41):
        t = math.pi * i / 40
        outline.append((half_length * math.cos(t), half_width * math.sin(t)))

    def place(x: float, y: float) -> tuple[float, float]:
        return (cx + x * math.cos(angle) - y * math.sin(angle), cy + x * math.sin(angle) + y * math.cos(angle))

    c.polygon([place(x, y) for x, y in outline], color)
    c.line([place(-half_length * 0.75, 0), place(half_length * 0.85, 0)], max(2.0, size / 18), BG)
    c.line([place(-half_length * 1.05, 0), place(-half_length * 0.75, 0)], max(2.0, size / 14), color)


def sun(c: Canvas, cx: float, cy: float, r: float) -> None:
    c.circle(cx, cy, r * 0.42, ACCENT)
    for i in range(8):
        a = math.pi / 4 * i
        inner, outer = r * 0.62, r * 0.9
        c.line(
            [(cx + inner * math.cos(a), cy + inner * math.sin(a)), (cx + outer * math.cos(a), cy + outer * math.sin(a))],
            r * 0.12,
            ACCENT,
        )


def cloud(c: Canvas, cx: float, cy: float, r: float, color: str = TEXT) -> None:
    c.circle(cx - r * 0.45, cy + r * 0.1, r * 0.38, color)
    c.circle(cx - r * 0.05, cy - r * 0.15, r * 0.5, color)
    c.circle(cx + r * 0.45, cy + r * 0.12, r * 0.35, color)
    c.rounded((cx - r * 0.83, cy + r * 0.1, cx + r * 0.8, cy + r * 0.48), r * 0.19, color)


def weather_icon(c: Canvas, condition: str, cx: float, cy: float, r: float, bg: str = CARD) -> None:
    if condition == "sunny":
        sun(c, cx, cy, r)
    elif condition == "partly":
        sun(c, cx + r * 0.28, cy - r * 0.28, r * 0.7)
        cloud(c, cx - r * 0.12, cy + r * 0.2, r * 0.86, bg)  # cut-out so the sun sits behind
        cloud(c, cx - r * 0.12, cy + r * 0.2, r * 0.72)
    elif condition == "fog":
        for i, width in enumerate((0.9, 0.7, 0.9)):
            y = cy + (i - 1) * r * 0.42
            c.line([(cx - r * width, y), (cx + r * width, y)], r * 0.16, MUTED)
    elif condition in ("rain", "storm", "snow"):
        cloud(c, cx, cy - r * 0.22, r * 0.9)
        if condition == "rain":
            for i in (-1, 0, 1):
                x = cx + i * r * 0.38
                c.line([(x + r * 0.08, cy + r * 0.42), (x - r * 0.08, cy + r * 0.78)], r * 0.12, RAIN)
        elif condition == "storm":
            bolt = [(0.05, 0.25), (-0.22, 0.62), (0.0, 0.62), (-0.12, 0.98), (0.26, 0.5), (0.04, 0.5), (0.18, 0.25)]
            c.polygon([(cx + x * r, cy + y * r) for x, y in bolt], ACCENT)
        else:
            for i in (-1, 0, 1):
                c.circle(cx + i * r * 0.38, cy + r * (0.6 if i else 0.75), r * 0.09, TEXT)
    else:
        cloud(c, cx, cy, r)


def mic(c: Canvas, cx: float, cy: float, size: float, color: str = MUTED) -> None:
    w, h = size * 0.36, size * 0.56
    c.rounded((cx - w / 2, cy - size / 2, cx + w / 2, cy - size / 2 + h), w / 2, color)
    stroke = max(2.0, size / 12)
    arc_box = (cx - size * 0.34, cy - size * 0.32, cx + size * 0.34, cy + size * 0.2)
    c.draw.arc(Canvas._s(arc_box), start=0, end=180, fill=color, width=round(stroke * SCALE))
    c.line([(cx, cy + size * 0.2), (cx, cy + size * 0.42)], stroke, color)
    c.line([(cx - size * 0.18, cy + size * 0.44), (cx + size * 0.18, cy + size * 0.44)], stroke, color)


# --- text helpers --------------------------------------------------------------

_SHORT_LABELS = [
    ("skip", "Skip water"),
    ("shade", "Shade"),
    ("deep", "Deep water"),
    ("neem", "Neem spray"),
    ("pest", "Pest check"),
    ("harvest", "Harvest"),
    ("prune", "Prune"),
    ("pinch", "Pinch"),
    ("mulch", "Mulch"),
    ("feed", "Feed"),
    ("fertil", "Feed"),
    ("repot", "Repot"),
    ("topsoil", "Loosen soil"),
    ("cut back", "Dry out"),
    ("soil", "Check soil"),
    ("water", "Water"),
]


def short_label(day: PlanDay | None) -> str:
    """One or two words for a day tile."""
    if day is None:
        return "—"
    if not day.tasks:
        return "Rest"
    action = day.tasks[0].action.lower()
    for keyword, label in _SHORT_LABELS:
        if keyword in action:
            return label
    return " ".join(day.tasks[0].action.split()[:2]).capitalize()


def weather_pill_text(weather: DayForecast | None) -> str:
    if weather is None:
        return "No forecast"
    if weather.rain_start_hour is not None:
        rain = f"Rain at {format_hour(weather.rain_start_hour)}"
    elif weather.rain_probability >= 30:
        rain = f"{weather.rain_probability}% rain"
    else:
        rain = "No rain"
    return f"{weather.temp_max:.0f}°  ·  {rain}"


# --- layout --------------------------------------------------------------------


@dataclass
class _CardLayout:
    task: PlanTask
    action_lines: list[str]
    reason_lines: list[str]
    action_size: int

    @property
    def height(self) -> float:
        action_line = self.action_size * 1.1
        return CARD_PAD + 28 + 18 + len(self.action_lines) * action_line + 14 + len(self.reason_lines) * 44 + CARD_PAD


def _layout_cards(tasks: Sequence[PlanTask]) -> list[_CardLayout]:
    """Pick the largest action size that fits, preferring every action on a single line."""
    inner = WIDTH - 2 * MARGIN - 2 * CARD_PAD
    available = CARDS_BOTTOM - CARDS_TOP
    attempts = [(1, 2, size) for size in ACTION_SIZES[:-1]] + [(2, 2, size) for size in ACTION_SIZES]
    attempts += [(2, 1, ACTION_SIZES[-1])]
    for action_lines, reason_lines, size in attempts:
        cards = [
            _CardLayout(
                task=task,
                action_lines=wrap(task.action, heading_font(size), inner, 2),
                reason_lines=wrap(task.reason, body_font(32), inner, reason_lines) if task.reason else [],
                action_size=size,
            )
            for task in tasks
        ]
        single_line = all(len(card.action_lines) <= action_lines for card in cards)
        if single_line and sum(card.height for card in cards) + CARD_GAP * (len(cards) - 1) <= available:
            return cards
    return cards


def _draw_header(c: Canvas, date: dt.date, weather: DayForecast | None) -> None:
    leaf(c, MARGIN + 22, HEADER_Y, 46)
    c.text((MARGIN + 62, HEADER_Y), "Today in the garden", heading_font(66), TEXT, anchor="lm")

    pill_font = body_font(32, 600)
    label = weather_pill_text(weather)
    pill_h = 72
    pill_w = 20 + 44 + 14 + width_of(label, pill_font) + 30
    top = PILL_Y - pill_h / 2
    c.rounded((MARGIN, top, MARGIN + pill_w, top + pill_h), pill_h / 2, CARD)
    weather_icon(c, weather.condition if weather else "cloudy", MARGIN + 20 + 22, PILL_Y, 22)
    c.text((MARGIN + 20 + 44 + 14, PILL_Y), label, pill_font, TEXT, anchor="lm")
    c.text((WIDTH - MARGIN, PILL_Y), f"{date:%a} {date.day} {date:%b}", body_font(32, 500), MUTED, anchor="rm")


def _draw_cards(c: Canvas, tasks: Sequence[PlanTask]) -> None:
    if not tasks:
        tasks = [PlanTask(plant="All clear", action="Nothing needed today", reason="Enjoy the garden.")]
    y = CARDS_TOP
    left, right = MARGIN, WIDTH - MARGIN
    for card in _layout_cards(tasks):
        c.rounded((left, y, right, y + card.height), 40, CARD)
        x = left + CARD_PAD
        cursor = y + CARD_PAD
        c.tracked((x, cursor), card.task.plant.upper(), body_font(26, 700), ACCENT, tracking=3)
        cursor += 28 + 18
        for line in card.action_lines:
            c.text((x, cursor), line, heading_font(card.action_size), TEXT)
            cursor += card.action_size * 1.1
        cursor += 14
        for line in card.reason_lines:
            c.text((x, cursor), line, body_font(32), MUTED)
            cursor += 44
        y += card.height + CARD_GAP


def _draw_strip(c: Canvas, upcoming: Sequence[tuple[dt.date, DayForecast | None, PlanDay | None]]) -> None:
    c.tracked((MARGIN, STRIP_LABEL_Y), "NEXT 6 DAYS", body_font(22, 700), MUTED, tracking=3)
    gap = 14
    tile_w = (WIDTH - 2 * MARGIN - gap * 5) / 6
    label_font = body_font(22, 500)
    for i, (date, weather, plan_day) in enumerate(list(upcoming)[:6]):
        x = MARGIN + i * (tile_w + gap)
        center = x + tile_w / 2
        c.rounded((x, STRIP_TOP, x + tile_w, STRIP_TOP + TILE_HEIGHT), 26, CARD)
        c.text((center, STRIP_TOP + 34), f"{date:%a}", body_font(26, 600), TEXT, anchor="mm")
        weather_icon(c, weather.condition if weather else "cloudy", center, STRIP_TOP + 92, 28)
        lines = wrap(short_label(plan_day), label_font, tile_w - 16, 2)
        first = STRIP_TOP + (152 if len(lines) == 1 else 142)
        for n, line in enumerate(lines):
            c.text((center, first + n * 26), line, label_font, MUTED, anchor="mm")


def _draw_footer(c: Canvas) -> None:
    text = "Done? Send Tendril a voice note."
    font = body_font(32, 500)
    total = 34 + 14 + width_of(text, font)
    left = (WIDTH - total) / 2
    mic(c, left + 17, FOOTER_Y, 34)
    c.text((left + 34 + 14, FOOTER_Y), text, font, MUTED, anchor="lm")


def render_plan_image(
    date: dt.date,
    weather: DayForecast | None,
    tasks: Sequence[PlanTask],
    upcoming: Sequence[tuple[dt.date, DayForecast | None, PlanDay | None]],
) -> Image.Image:
    """Today's plan: header and weather pill, up to two task cards, a 6-day strip and a footer."""
    canvas = Canvas()
    _draw_header(canvas, date, weather)
    _draw_cards(canvas, tasks)
    _draw_strip(canvas, upcoming)
    _draw_footer(canvas)
    return canvas.finish()


# Phones since about 2017 are 19.5:9, taller than the 9:16 plan. The lock-screen copy adds
# plain background so it fills the screen without cropping: a little on top (more room for
# the clock) and the rest at the bottom, clear of the flashlight and camera buttons.
LOCKSCREEN_HEIGHT = round(WIDTH * 19.5 / 9)
LOCKSCREEN_TOP_PAD = 140


def lockscreen_image(plan: Image.Image) -> Image.Image:
    canvas = Image.new("RGB", (WIDTH, LOCKSCREEN_HEIGHT), BG)
    canvas.paste(plan, (0, LOCKSCREEN_TOP_PAD))
    return canvas


def save_plan_image(image: Image.Image, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, "PNG", optimize=True)
    return path
