"""Turn a voice note into facts, tick off matching tasks, and flag serious problems."""

import datetime as dt
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_sessionmaker
from app.models import CheckIn, Garden, Plant, Task, TaskStatus
from app.schemas import FACTS_SCHEMA, CheckInFacts
from app.services.llm import LLM, OllamaClient, ask_validated
from app.services.plans import replan_rest_of_week
from app.services.rules import match_plant, serious_issues
from app.services.weather import WeatherError

log = logging.getLogger(__name__)

SYSTEM_PROMPT = "You extract facts from a gardener's short voice note. Reply with JSON only."

_DONE_WORDS = {
    "water": "watered",
    "harvest": "harvested",
    "prun": "pruned",
    "pinch": "pinched",
    "spray": "sprayed",
    "neem": "sprayed neem",
    "fertili": "fed",
    "feed": "fed",
    "fed ": "fed",
    "mulch": "mulched",
    "repot": "repotted",
    "weed": "weeded",
    "shade": "moved to shade",
}
_NEGATIONS = ("didn't", "did not", "not ", "haven't", "have not", "forgot", "skipped", "nahi")
_STOP = {"the", "and", "for", "with", "into", "from", "today", "some", "all", "its", "their", "them", "plant", "plants"}
_ALL_DONE = {"all", "everything", "all tasks", "all done", "done", "both"}


@dataclass
class CheckInResult:
    check_in: CheckIn
    facts: CheckInFacts
    marked: list[Task]
    serious: list[str]
    source: str  # "model" or "rules"

    @property
    def replan_needed(self) -> bool:
        """A serious problem on a known plant; with no plant, Tendril asks rather than guessing."""
        return bool(self.serious and self.facts.plant)

    @property
    def message(self) -> str:
        parts = []
        if self.marked:
            actions = ", ".join(f"“{task.action}”" for task in self.marked)
            parts.append(f"Marked {actions} done.")
        if self.replan_needed:
            parts.append(
                f"Noted {' and '.join(self.serious)} on {self.facts.plant}, so the rest of the week is being re-planned."
            )
        elif self.serious:
            parts.append(
                f"Noted {' and '.join(self.serious)}. Which plant was it? "
                "Send a quick note naming it, and Tendril will plan the treatment."
            )
        elif self.facts.observations:
            parts.append(f"Noted: {self.facts.observations[0]}.")
        return " ".join(parts) or "Thanks, noted."


def build_prompt(plants: Sequence[Plant], today_tasks: Sequence[Task], transcript: str) -> str:
    plant_list = ", ".join(f"{p.name} ({p.location_type})" for p in plants) or "none"
    task_list = "; ".join(f"{t.plant.name}: {t.action}" for t in today_tasks) or "none"
    return f"""The gardener's plants: {plant_list}.
Today's tasks: {task_list}.
Voice note (may be in English or Hindi): "{transcript}"

Extract facts, written in short English phrases:
- plant: the plant the note is mainly about, using a name above, or "" if unclear or the whole garden
- done: care they say they already did, naming the plant when they do, e.g. "watered chillies", "harvested tulsi leaves"
- observations: other things they noticed, e.g. "new flowers"
- health_flags: problems such as wilting, pests, aphids, rot, mould, yellow leaves; [] if none

Reply with JSON only: {{"plant": "", "done": [], "observations": [], "health_flags": []}}"""


def _normalise(facts: CheckInFacts, plants: Sequence[Plant]) -> CheckInFacts:
    plant = match_plant(facts.plant, plants) if facts.plant else None
    return facts.model_copy(update={"plant": plant.name if plant else ""})


def _mentioned(text: str, plants: Sequence[Plant]) -> list[str]:
    """Plants named in the text. Whole names first, so "tomatillo" isn't also read as "tomato"."""
    whole = [p.name for p in plants if p.name.lower().rstrip("s") in text]
    return whole or [p.name for p in plants if p.name.lower()[:5] in text]


def _sentences(text: str) -> list[str]:
    return [f" {part} " for part in re.split(r"[.!?;,]|and|but", text.lower()) if part.strip()]


def keyword_done(transcript: str, plants: Sequence[Plant]) -> list[str]:
    """Care actions said in the note, with the plant named in the same sentence ("watered chillies")."""
    items = []
    for sentence in _sentences(transcript):
        if any(word in sentence for word in _NEGATIONS):
            continue
        named = _mentioned(sentence, plants)
        for word, label in _DONE_WORDS.items():
            if word in sentence:
                items.append(f"{label} {named[0].lower()}" if len(named) == 1 else label)
    return list(dict.fromkeys(items))


def rule_based_facts(transcript: str, plants: Sequence[Plant]) -> CheckInFacts:
    """Keyword fallback when the model is unavailable."""
    text = f" {transcript.lower()} "
    done = keyword_done(transcript, plants)
    flags = serious_issues([text])
    if "yellow" in text:
        flags.append("yellow leaves")

    # A problem belongs to the plant named in the same sentence; otherwise use the only plant named.
    plant = ""
    mentioned = _mentioned(text, plants)
    if flags:
        for sentence in re.split(r"[.!?;,]|\band\b|\bbut\b", text):
            if serious_issues([sentence]) or "yellow" in sentence:
                plant = next(iter(_mentioned(sentence, plants)), "")
                break
    if not plant and len(mentioned) == 1:
        plant = mentioned[0]

    observations = [] if done or flags else [transcript.strip()[:120]]
    return CheckInFacts(plant=plant, done=done, observations=observations, health_flags=flags)


async def extract_facts(
    transcript: str, plants: Sequence[Plant], today_tasks: Sequence[Task], llm: LLM | None = None
) -> tuple[CheckInFacts, str]:
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": build_prompt(plants, today_tasks, transcript)},
    ]
    facts = await ask_validated(
        llm or OllamaClient(), messages, FACTS_SCHEMA, lambda data: _normalise(CheckInFacts.model_validate(data), plants)
    )
    if facts is None:
        log.warning("Using keyword extraction instead of the model")
        return rule_based_facts(transcript, plants), "rules"
    # Small models sometimes leave out an action; keywords catch the obvious ones.
    extra = [item for item in keyword_done(transcript, plants) if not _already_said(item, facts, plants)]
    return facts.model_copy(update={"done": [*facts.done, *extra][:10]}), "model"


def _already_said(item: str, facts: CheckInFacts, plants: Sequence[Plant]) -> bool:
    """Is a keyword item ("watered chillies") already covered by the model's done list?"""
    plant_words = set().union(*(_content_words(p.name) for p in plants)) if plants else set()
    action = _content_words(item) - plant_words
    named = _mentioned(f" {item} ", plants)
    for said in facts.done:
        if not action <= _content_words(said):
            continue
        said_named = _mentioned(f" {said.lower()} ", plants)
        said_plant = said_named[0] if said_named else facts.plant
        if not named or not said_plant or named[0] == said_plant:
            return True
    return False


def _stem(word: str) -> str:
    for suffix in ("ing", "ed", "es", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)]
    return word


def _content_words(text: str) -> set[str]:
    return {_stem(word) for word in re.findall(r"[a-z]+", text.lower()) if len(word) > 2 and word not in _STOP}


# What kind of job an action is, so "loosened the soil" can't tick off "Check soil moisture"
# just because both mention soil. First match wins, so specific kinds come before general ones.
_ACTION_TYPES = [
    ("skip_water", r"\bskip\w*\b.*\bwater|\bno water"),
    ("less_water", r"\bcut back on water|\bless water|\breduc\w* water"),
    ("deep_water", r"\bdeep\w*\b.*\bwater|\bwater\w*\b.*\bdeep|\bsoak"),
    ("shade", r"\bshade"),
    ("pest_treat", r"\bneem|\bspray"),
    ("pest_check", r"\bpest|\baphid|\bbug|\binsect"),
    ("soil_check", r"\bcheck\w*\b.*\bsoil|\bsoil moisture|\bfinger test"),
    ("loosen_soil", r"\bloosen|\btopsoil|\bhoe\b|\btill\w*\b"),
    ("prune", r"\bprun|\bpinch|\btrim|\bdeadhead|\byellow leaves|\bremov\w*\b.*\bleaves"),
    ("harvest", r"\bharvest|\bpick\w*\b"),
    ("feed", r"\bfeed|\bfed\b|\bfertili|\bcompost|\bmanure"),
    ("mulch", r"\bmulch"),
    ("repot", r"\brepot|\btransplant"),
    ("weed", r"\bweed"),
    ("support", r"\bstak\w*\b|\btie\b|\btied\b|\btying\b|\btrellis|\bsupport"),
    ("water", r"\bwater"),
]
# Saying "watered" is close enough to tick off a deep watering, and the other way round.
_SAME_JOB = {"water": {"water", "deep_water"}, "deep_water": {"water", "deep_water"}}


def action_type(text: str) -> str | None:
    lowered = text.lower()
    return next((kind for kind, pattern in _ACTION_TYPES if re.search(pattern, lowered)), None)


def _main_verb(text: str) -> str:
    words = re.findall(r"[a-z]+", text.lower())
    return _stem(words[0]).rstrip("e") if words else ""  # "rotate" and "rotated" -> "rotat"


def matches_task(task: Task, done: Sequence[str], plants: Sequence[Plant] = (), default_plant: str = "") -> bool:
    """Does something the gardener said they did tick off this task?

    An item naming a plant ("watered chillies") only counts for that plant; other items count
    for the note's main plant, or for every plant when the note isn't about one. It also has to
    be the same kind of job; for actions outside the known kinds, the main verb must match.
    """
    task_type = action_type(task.action)
    for item in done:
        said = item.lower().strip()
        named = _mentioned(f" {said} ", plants)
        if named and task.plant.name not in named:
            continue
        if not named and default_plant and task.plant.name != default_plant:
            continue
        if said in _ALL_DONE:
            return True
        said_type = action_type(said)
        if task_type and said_type:
            if said_type in _SAME_JOB.get(task_type, {task_type}):
                return True
        elif task_type is None and said_type is None and _main_verb(task.action) == _main_verb(said):
            return True
    return False


async def record_check_in(
    session: AsyncSession,
    garden: Garden,
    *,
    transcript: str,
    now: dt.datetime,
    audio_path: str | None = None,
    llm: LLM | None = None,
) -> CheckInResult:
    today = now.date()
    plant_ids = [plant.id for plant in garden.plants]
    today_tasks = list(
        await session.scalars(select(Task).where(Task.plant_id.in_(plant_ids), Task.date == today).order_by(Task.id))
    )
    facts, source = await extract_facts(transcript, garden.plants, today_tasks, llm)

    marked = []
    for task in today_tasks:
        if task.status != TaskStatus.pending:
            continue
        if matches_task(task, facts.done, garden.plants, facts.plant):
            task.status = TaskStatus.done
            task.completed_at = dt.datetime.now(dt.timezone.utc)
            marked.append(task)

    check_in = CheckIn(
        garden_id=garden.id, date=today, audio_path=audio_path, transcript=transcript, facts=facts.model_dump()
    )
    session.add(check_in)
    await session.commit()
    serious = serious_issues(facts.health_flags)
    log.info("Check-in for %s: %d task(s) done, serious=%s, via %s", garden.name, len(marked), serious, source)
    return CheckInResult(check_in=check_in, facts=facts, marked=marked, serious=serious, source=source)


async def prune_recordings(session: AsyncSession, older_than_days: int, now: dt.datetime | None = None) -> int:
    """Delete voice recordings older than the retention window. Transcripts and facts stay.

    Also removes stray files that never became a check-in (a failed transcription, for example).
    Returns how many recordings were deleted.
    """
    if older_than_days <= 0:
        return 0
    cutoff = (now or dt.datetime.now(dt.timezone.utc)) - dt.timedelta(days=older_than_days)
    removed = 0
    old = await session.scalars(
        select(CheckIn).where(CheckIn.audio_path.is_not(None), CheckIn.created_at < cutoff)
    )
    for check_in in old:
        Path(check_in.audio_path).unlink(missing_ok=True)
        check_in.audio_path = None
        removed += 1
    await session.commit()

    audio_dir = get_settings().audio_dir
    if audio_dir.is_dir():
        still_used = set(await session.scalars(select(CheckIn.audio_path).where(CheckIn.audio_path.is_not(None))))
        for path in audio_dir.iterdir():
            if not path.is_file() or str(path) in still_used:
                continue
            modified = dt.datetime.fromtimestamp(path.stat().st_mtime, dt.timezone.utc)
            if modified < cutoff:
                path.unlink(missing_ok=True)
                removed += 1
    return removed


async def replan_after_check_in(garden_id: int, today: dt.date, llm: LLM | None = None) -> None:
    """Background job: re-plan the rest of the week with its own database session."""
    async with get_sessionmaker()() as session:
        garden = await session.get(Garden, garden_id)
        if garden is None:
            return
        try:
            await replan_rest_of_week(session, garden, today=today, llm=llm)
        except WeatherError as exc:
            log.warning("Could not re-plan after check-in: %s", exc)
