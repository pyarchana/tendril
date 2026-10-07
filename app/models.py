import datetime as dt
import enum
import secrets

from sqlalchemy import JSON, Date, DateTime, Enum, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def new_token() -> str:
    """Random, unguessable token used in the Today and check-in page URLs."""
    return secrets.token_urlsafe(16)


class LocationType(enum.StrEnum):
    pot = "pot"
    bed = "bed"
    terrace = "terrace"
    window = "window"


class TaskStatus(enum.StrEnum):
    pending = "pending"
    done = "done"
    skipped = "skipped"


class Garden(Base):
    __tablename__ = "gardens"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    lat: Mapped[float]
    lon: Mapped[float]
    timezone: Mapped[str] = mapped_column(String(64))
    checkin_token: Mapped[str] = mapped_column(String(64), unique=True, default=new_token)
    last_nudge_on: Mapped[dt.date | None] = mapped_column(Date, default=None)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    plants: Mapped[list["Plant"]] = relationship(
        back_populates="garden", cascade="all, delete-orphan", lazy="selectin", order_by="Plant.id"
    )


class Plant(Base):
    __tablename__ = "plants"

    id: Mapped[int] = mapped_column(primary_key=True)
    garden_id: Mapped[int] = mapped_column(ForeignKey("gardens.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(100))
    species: Mapped[str] = mapped_column(String(200), default="")
    location_type: Mapped[LocationType] = mapped_column(Enum(LocationType, native_enum=False))
    notes: Mapped[str] = mapped_column(Text, default="")

    garden: Mapped[Garden] = relationship(back_populates="plants")
    tasks: Mapped[list["Task"]] = relationship(back_populates="plant", cascade="all, delete-orphan")


class WeekPlan(Base):
    __tablename__ = "week_plans"
    __table_args__ = (UniqueConstraint("garden_id", "week_start"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    garden_id: Mapped[int] = mapped_column(ForeignKey("gardens.id", ondelete="CASCADE"))
    week_start: Mapped[dt.date] = mapped_column(Date)
    forecast: Mapped[dict] = mapped_column(JSON, default=dict)
    days: Mapped[list] = mapped_column(JSON, default=list)
    # "model" when the LLM produced the plan, "rules" when we fell back.
    source: Mapped[str] = mapped_column(String(20), default="model")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(primary_key=True)
    plant_id: Mapped[int] = mapped_column(ForeignKey("plants.id", ondelete="CASCADE"))
    date: Mapped[dt.date] = mapped_column(Date, index=True)
    action: Mapped[str] = mapped_column(String(100))
    reason: Mapped[str] = mapped_column(String(300), default="")
    status: Mapped[TaskStatus] = mapped_column(
        Enum(TaskStatus, native_enum=False), default=TaskStatus.pending
    )
    completed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    plant: Mapped[Plant] = relationship(back_populates="tasks", lazy="joined")


class CheckIn(Base):
    __tablename__ = "check_ins"

    id: Mapped[int] = mapped_column(primary_key=True)
    garden_id: Mapped[int] = mapped_column(ForeignKey("gardens.id", ondelete="CASCADE"))
    date: Mapped[dt.date] = mapped_column(Date, index=True)
    audio_path: Mapped[str | None] = mapped_column(String(300), default=None)
    transcript: Mapped[str] = mapped_column(Text, default="")
    facts: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
