"""Tendril demo: seed two plants, plan the week with the local model, render today's plan.

Usage:
  python -m scripts.demo                    seed (if needed), plan the week, render today's image
  python -m scripts.demo --reset            start again from a fresh demo garden
  python -m scripts.demo --note "text"      a typed check-in
  python -m scripts.demo --voice note.wav   a voice check-in through faster-whisper

In Docker: make demo  (or docker compose exec app python -m scripts.demo)
"""

import argparse
import asyncio
import json
import time
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import dispose_engine, get_sessionmaker, init_db
from app.models import Garden
from app.services.checkin import record_check_in
from app.services.plans import create_week_plan, garden_now, replan_rest_of_week
from app.services.speech import SpeechError, WhisperTranscriber
from app.services.today import ensure_plan_image, load_today
from scripts.seed import seed


async def plan_and_render(session: AsyncSession, garden: Garden) -> None:
    model = get_settings().ollama_model
    print(f"Planning the week with {model}. On a CPU this can take a minute...")
    started = time.monotonic()
    plan = await create_week_plan(session, garden)
    took = time.monotonic() - started
    if plan.source == "model":
        print(f"Plan made by {model} in {took:.0f}s.")
    else:
        print(f"{model} wasn't available, so the rule-based planner made the plan ({took:.0f}s).")
        print("If the model is still downloading, watch it with: docker compose logs -f ollama-pull")
    await show_today(session, garden)


async def show_today(session: AsyncSession, garden: Garden) -> None:
    view = await load_today(session, garden, garden_now(garden))
    image = await ensure_plan_image(view)
    print(f"\nToday: {view.summary}")
    for task in view.tasks:
        print(f"  [{task.status}] {task.plant.name}: {task.action} ({task.reason})")
    print(f"Plan image: {image}")


async def check_in(session: AsyncSession, garden: Garden, note: str | None, voice: str | None) -> None:
    if voice:
        print("Transcribing with faster-whisper (the first run downloads the model)...")
        started = time.monotonic()
        hint = "Garden check-in about " + ", ".join(plant.name for plant in garden.plants) + "."
        try:
            note = await WhisperTranscriber().transcribe(Path(voice), hint=hint)
        except SpeechError as exc:
            raise SystemExit(f"Couldn't transcribe {voice}: {exc}") from exc
        print(f'Heard in {time.monotonic() - started:.0f}s: "{note}"')
    now = garden_now(garden)
    result = await record_check_in(session, garden, transcript=note or "", now=now, audio_path=voice)
    print(f"Tendril: {result.message}")
    print(f"Facts ({result.source}): {json.dumps(result.facts.model_dump())}")
    if result.replan_needed:
        print("Re-planning the rest of the week...")
        plan = await replan_rest_of_week(session, garden, today=now.date())
        print(f"Re-planned by {plan.source}.")
    await show_today(session, garden)


async def main(args: argparse.Namespace) -> None:
    settings = get_settings()
    settings.ensure_dirs()
    await init_db()
    async with get_sessionmaker()() as session:
        garden = await seed(session, reset=args.reset)
        print(f"Garden: {garden.name} with {', '.join(p.name for p in garden.plants)}")
        if args.note or args.voice:
            await check_in(session, garden, args.note, args.voice)
        else:
            await plan_and_render(session, garden)
        base = settings.public_base_url.rstrip("/")
        print(f"\nOpen the Today page: {base}/today/{garden.checkin_token}")
        print(f"Voice check-in page: {base}/c/{garden.checkin_token}")
    await dispose_engine()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the Tendril demo.")
    parser.add_argument("--reset", action="store_true", help="start from a fresh demo garden")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--note", help="send a typed check-in instead of planning")
    group.add_argument("--voice", help="send an audio file as a voice check-in")
    asyncio.run(main(parser.parse_args()))
