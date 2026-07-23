"""The personal 'about me' pitch / interview script (global base version).

Stored as a plain Markdown/text file (``config.PITCH_PATH``) so it is easy to
edit, back up and listen to. The real content is kept ONLY in that local,
git-ignored file (``data/pitch.md``) — never hard-coded here — so personal
scripts are never committed. On first use the file is seeded with the neutral
template below, which you then replace with your own pitch in the app.

A styled HTML twin (``config.PITCH_HTML_PATH``) can also be kept for reading
in the app — imported from the user's Resume folder on first use.
"""
from __future__ import annotations

import re
from pathlib import Path

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

# Default styled interview script (Hebrew) — copied into the profile on first use.
DEFAULT_PITCH_HTML_SOURCE = Path(
    "/Users/ramizilbershmit/MyDocuments/Resume/סקריפט ראיון - מאוחד.html"
)


def load_base_pitch() -> str:
    """Return the global base pitch, seeding the file on first use."""
    ensure_html()  # may also seed pitch.md from the HTML body
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


def pitch_html_path() -> Path:
    return config.PITCH_HTML_PATH


def has_html() -> bool:
    return pitch_html_path().is_file()


def load_html() -> str:
    """Return the styled pitch HTML (empty string if missing)."""
    ensure_html()
    path = pitch_html_path()
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def save_html(html: str, *, sync_text: bool = True) -> None:
    """Persist the styled HTML; optionally sync plain text into pitch.md."""
    html = html or ""
    pitch_html_path().write_text(html, encoding="utf-8")
    if sync_text and html.strip():
        plain = html_to_plain_text(html)
        if plain.strip():
            save_base_pitch(plain)


def html_to_plain_text(html: str) -> str:
    """Visible text from the pitch HTML — for TTS, AI, and simple text edit."""
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html or "", "html.parser")
        for tag in soup(["script", "style"]):
            tag.decompose()
        root = soup.find(class_="wrap") or soup.body or soup
        text = root.get_text("\n", strip=True)
    except Exception:
        text = re.sub(r"(?is)<script.*?>.*?</script>", " ", html or "")
        text = re.sub(r"(?is)<style.*?>.*?</style>", " ", text)
        text = re.sub(r"(?s)<[^>]+>", "\n", text)
    # Collapse excess blank lines
    lines = [ln.rstrip() for ln in text.splitlines()]
    out: list[str] = []
    blank = False
    for ln in lines:
        if not ln.strip():
            if not blank and out:
                out.append("")
            blank = True
        else:
            out.append(ln.strip())
            blank = False
    return "\n".join(out).strip() + ("\n" if out else "")


def ensure_html() -> Path | None:
    """Ensure profile ``pitch.html`` exists; import from Resume folder if needed.

    Also seeds ``pitch.md`` from the HTML body when the markdown file is still
    missing or still the neutral seed template.
    """
    dest = pitch_html_path()
    if not dest.is_file():
        src = DEFAULT_PITCH_HTML_SOURCE
        if src.is_file():
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
            except OSError:
                return None
        else:
            return None

    # Keep pitch.md aligned with the HTML on first import / seed state.
    try:
        md = config.PITCH_PATH
        html = dest.read_text(encoding="utf-8")
        plain = html_to_plain_text(html)
        if not plain.strip():
            return dest
        if not md.exists():
            md.write_text(plain, encoding="utf-8")
        else:
            current = md.read_text(encoding="utf-8")
            if current.strip() == SEED_PITCH.strip():
                md.write_text(plain, encoding="utf-8")
    except OSError:
        pass
    return dest


# --------------------------------------------------------------------------- #
# AI-revision draft: a pending rewrite of the base pitch, kept in its own file
# so the user can review it side-by-side (with diff highlighting) before it
# replaces the memorized original.

def _draft_path():
    return config.PITCH_PATH.with_name("pitch_draft.md")


def load_draft() -> str:
    try:
        return _draft_path().read_text(encoding="utf-8")
    except OSError:
        return ""


def save_draft(text: str) -> None:
    _draft_path().write_text(text or "", encoding="utf-8")


def clear_draft() -> None:
    _draft_path().unlink(missing_ok=True)


def has_draft() -> bool:
    return bool(load_draft().strip())
