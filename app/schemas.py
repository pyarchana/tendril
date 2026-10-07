"""Shapes the model must return. Anything that fails these is retried once, then replaced by rules."""

import datetime as dt

from pydantic import BaseModel, Field, field_validator

MAX_TASKS_PER_DAY = 2
MAX_ACTION_WORDS = 5  # "each action under 6 words"


class PlanTask(BaseModel):
    plant: str = Field(min_length=1, max_length=60)
    action: str = Field(min_length=1, max_length=60)
    reason: str = Field(default="", max_length=200)

    @field_validator("plant", "reason")
    @classmethod
    def _strip(cls, value: str) -> str:
        return value.strip()

    @field_validator("action")
    @classmethod
    def _short_action(cls, value: str) -> str:
        value = value.strip().rstrip(".")
        if len(value.split()) > MAX_ACTION_WORDS:
            raise ValueError(f"action '{value}' must be under 6 words")
        return value


class PlanDay(BaseModel):
    date: dt.date
    tasks: list[PlanTask] = Field(default_factory=list, max_length=MAX_TASKS_PER_DAY)


class WeekPlanReply(BaseModel):
    days: list[PlanDay]


class CheckInFacts(BaseModel):
    """What a voice note says: which plant, what was done, what was seen, any problems."""

    plant: str = Field(default="", max_length=60)
    done: list[str] = Field(default_factory=list, max_length=10)
    observations: list[str] = Field(default_factory=list, max_length=10)
    health_flags: list[str] = Field(default_factory=list, max_length=10)

    @field_validator("done", "observations", "health_flags")
    @classmethod
    def _clean_items(cls, items: list[str]) -> list[str]:
        return [item.strip()[:120] for item in items if item and item.strip()]


# Hand-written JSON schemas passed to Ollama's `format` to constrain generation.
_TASK_SCHEMA = {
    "type": "object",
    "properties": {
        "plant": {"type": "string"},
        "action": {"type": "string"},
        "reason": {"type": "string"},
    },
    "required": ["plant", "action", "reason"],
}
DAY_SCHEMA = {
    "type": "object",
    "properties": {
        "date": {"type": "string"},
        "tasks": {"type": "array", "items": _TASK_SCHEMA, "maxItems": MAX_TASKS_PER_DAY},
    },
    "required": ["date", "tasks"],
}
WEEK_SCHEMA = {
    "type": "object",
    "properties": {"days": {"type": "array", "items": DAY_SCHEMA}},
    "required": ["days"],
}
_STRINGS = {"type": "array", "items": {"type": "string"}}
FACTS_SCHEMA = {
    "type": "object",
    "properties": {"plant": {"type": "string"}, "done": _STRINGS, "observations": _STRINGS, "health_flags": _STRINGS},
    "required": ["plant", "done", "observations", "health_flags"],
}
