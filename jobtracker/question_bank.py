"""Curated Hebrew QA/automation interview material bundled with the app.

Two resources live in ``jobtracker/resources/``, distilled from Rami's own
prep documents:

- ``interview_questions_he.json`` — the classic "105 questions" bank of Hebrew
  software-testing interview questions with model answers. A random sample is
  woven into every Hebrew mock-interview simulation so each run feels like a
  real Israeli QA interview.
- ``qa_exercise_example_he.md`` — a fully worked "testing scenario" interview
  exercise (rule-based alerting system) used as the gold-standard exemplar
  when generating a new practice exercise for a specific job.
"""
from __future__ import annotations

import json
import random
from functools import lru_cache
from pathlib import Path

_RESOURCES = Path(__file__).resolve().parent / "resources"


@lru_cache(maxsize=1)
def load_interview_bank() -> list[dict]:
    """All bank questions: [{"id", "group", "q", "a"}, ...] (empty on error)."""
    try:
        data = json.loads(
            (_RESOURCES / "interview_questions_he.json").read_text(encoding="utf-8"))
        return [q for q in data.get("questions", []) if q.get("q")]
    except Exception:
        return []


def sample_interview_questions(n: int = 10) -> list[dict]:
    """A random, group-diverse sample of bank questions for one simulation."""
    bank = load_interview_bank()
    if len(bank) <= n:
        return list(bank)
    # Spread across groups so a simulation mixes warm-up/behavioural/technical.
    by_group: dict[str, list[dict]] = {}
    for q in bank:
        by_group.setdefault(q.get("group", ""), []).append(q)
    picked: list[dict] = []
    groups = list(by_group.values())
    while len(picked) < n and any(groups):
        for g in groups:
            if g and len(picked) < n:
                picked.append(g.pop(random.randrange(len(g))))
    return picked


@lru_cache(maxsize=1)
def load_exercise_example() -> str:
    """The worked QA-scenario exercise (Markdown), used as a style exemplar."""
    try:
        return (_RESOURCES / "qa_exercise_example_he.md").read_text(encoding="utf-8")
    except Exception:
        return ""
