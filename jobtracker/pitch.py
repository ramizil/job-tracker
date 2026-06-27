"""The personal 'about me' pitch / interview script (global base version).

Stored as a plain Markdown/text file (``config.PITCH_PATH``) so it is easy to
edit, back up and listen to. The real content is kept ONLY in that local,
git-ignored file (``data/pitch.md``) — never hard-coded here — so personal
scripts are never committed. On first use the file is seeded with the neutral
template below, which you then replace with your own pitch in the app.
"""
from __future__ import annotations

from . import config

# Neutral starter template — intentionally contains NO personal information.
# Your real pitch lives in the git-ignored data/pitch.md and stays private.
SEED_PITCH = """My pitch — the "5 stops" structure

Stop 1: Opening (who I am + the big picture)
Introduce yourself in one warm sentence: your field, years of experience, and
the kind of systems/teams you've worked with.

Stop 2: Day-to-day engineering (how the work actually happens)
Describe your typical day and your approach: the tools, methodologies and the
kinds of problems you solve.

Stop 3: Impact, innovation & numbers
Highlight initiatives you built that saved time or added value — quantify the
impact where you can (hours/days saved, tools adopted).

Stop 4: The passion (personal connection)
A short, genuine story about why you love this work.

Stop 5: Summary & looking forward (why I'm here)
What you're looking for next and why this role/company fits.

Tip: don't memorise words — memorise the 5 stops, then speak naturally.

Edit this in the app (My Pitch) to make it your own; it is saved locally only.
"""


def load_base_pitch() -> str:
    """Return the global base pitch, seeding the file on first use."""
    path = config.PITCH_PATH
    if not path.exists():
        try:
            path.write_text(SEED_PITCH, encoding="utf-8")
        except Exception:
            return SEED_PITCH
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return SEED_PITCH


def save_base_pitch(text: str) -> None:
    config.PITCH_PATH.write_text(text or "", encoding="utf-8")
