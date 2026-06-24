"""Score a job posting against the resume profile."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import resume as resume_mod


@dataclass
class MatchResult:
    score: float                 # 0-100
    matched: list[str]           # canonical skills found in the posting
    missing: list[str]           # profile skills NOT in the posting

    def as_summary(self) -> str:
        return f"{self.score:.0f}% | matched: {', '.join(self.matched) or '-'}"


def score_text(text: str, profile: dict[str, Any] | None = None) -> MatchResult:
    """Weighted keyword overlap between a posting and the resume profile."""
    profile = profile or resume_mod.load_profile()
    aliases = resume_mod.alias_map(profile)
    weights: dict[str, float] = profile.get("weights", {})

    hay = resume_mod.tokenize(text)

    matched: list[str] = []
    missing: list[str] = []
    got = 0.0
    total = 0.0

    for skill, alias_list in aliases.items():
        w = float(weights.get(skill, 1.0))
        total += w
        if any(a in hay for a in alias_list):
            matched.append(skill)
            got += w
        else:
            missing.append(skill)

    score = (got / total * 100.0) if total else 0.0
    return MatchResult(round(score, 1), sorted(matched), sorted(missing))


def score_job(title: str, description: str,
              profile: dict[str, Any] | None = None) -> MatchResult:
    """Title is weighted double (a keyword in the title matters more)."""
    return score_text(f"{title} {title} {description}", profile)
