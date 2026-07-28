"""The personal 'about me' pitches (global base versions).

Two kinds:
  * ``interview`` — full professional interview script (5 stops)
  * ``recruiter`` — short conversational screen pitch (~45–60s)

Styled HTML is the source of truth when present; the ``.md`` mirror is used
for TTS, AI and backups and stays in sync with the HTML body.
"""
from __future__ import annotations

import html as html_lib
import re
from pathlib import Path

from . import config

PITCH_KINDS = ("interview", "recruiter")
KIND_LABELS = {
    "interview": "Interview pitch",
    "recruiter": "Recruiter pitch",
}

# Neutral starter template — intentionally contains NO personal information.
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

DEFAULT_PITCH_HTML_SOURCE = Path(
    "/Users/ramizilbershmit/MyDocuments/Resume/סקריפט ראיון - מאוחד.html"
)
SEED_RECRUITER_HTML = Path(__file__).resolve().parent / "seeds" / "pitch_recruiter.html"

_SECTION_RE = re.compile(
    r"^(חלק\s+[^\n]+|Stop\s+\d+[^\n]*|Part\s+[A-Za-z0-9]+[^\n]*)$",
    re.I,
)
_STATION_RE = re.compile(
    r"^(\d{1,2})[\.\)\:]?\s+(.+)$",
)
_BULLET_RE = re.compile(r"^[\-•\*]\s+(.+)$")
_TIP_RE = re.compile(r"^(טיפ|Tip)\s*[:：\-–]?\s*(.+)$", re.I)

# Spoken openers that must never become the blue <header> banner.
_BANNER_OPENER_RE = re.compile(
    r"^(נעים מאוד|שלום|היי|בוקר טוב|hi[,!\s]|hello\b|i['’]?m\b|i am\b|my name\b)",
    re.I,
)


def _is_banner_title(line: str) -> bool:
    """True if line is a short document title — not spoken script prose."""
    s = (line or "").strip()
    if not s or len(s) > 90:
        return False
    if (_SECTION_RE.match(s) or _STATION_RE.match(s)
            or _BULLET_RE.match(s) or _TIP_RE.match(s)):
        return False
    if _BANNER_OPENER_RE.match(s):
        return False
    # Multi-sentence lines are body copy, not an h1.
    if s.count(".") + s.count("!") + s.count("?") >= 2:
        return False
    return True


def _is_banner_subtitle(line: str) -> bool:
    """True if line is a short lead under the title — not a script paragraph."""
    s = (line or "").strip()
    if not s or len(s) > 180:
        return False
    if (_SECTION_RE.match(s) or _STATION_RE.match(s)
            or _BULLET_RE.match(s) or _TIP_RE.match(s)):
        return False
    if _BANNER_OPENER_RE.match(s):
        return False
    # Dense spoken paragraphs (tech lists) belong in quote cards.
    if len(s) > 110 and (s.count(",") >= 3 or s.count("·") + s.count("•") >= 2):
        return False
    return True


def normalize_kind(kind: str | None) -> str:
    k = (kind or "interview").strip().lower()
    if k in ("rec", "recruiter", "screen", "screening", "phone", "hr"):
        return "recruiter"
    return "interview"


def pitch_md_path(kind: str = "interview") -> Path:
    if normalize_kind(kind) == "recruiter":
        return config.PITCH_RECRUITER_PATH
    return config.PITCH_PATH


def pitch_html_path(kind: str = "interview") -> Path:
    if normalize_kind(kind) == "recruiter":
        return config.PITCH_RECRUITER_HTML_PATH
    return config.PITCH_HTML_PATH


def has_html(kind: str = "interview") -> bool:
    return pitch_html_path(kind).is_file()


def _write_md(text: str, kind: str = "interview") -> None:
    path = pitch_md_path(kind)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text or "", encoding="utf-8")


def load_base_pitch(kind: str = "interview") -> str:
    """Return the pitch as plain text — from HTML when present (source of truth)."""
    kind = normalize_kind(kind)
    ensure_html(kind)
    if has_html(kind):
        plain = html_to_plain_text(_read_html_raw(kind))
        if plain.strip():
            try:
                _write_md(plain, kind)
            except OSError:
                pass
            return plain
    path = pitch_md_path(kind)
    if not path.exists():
        seed = SEED_PITCH if kind == "interview" else ""
        if kind == "recruiter" and SEED_RECRUITER_HTML.is_file():
            try:
                seed = html_to_plain_text(
                    SEED_RECRUITER_HTML.read_text(encoding="utf-8"))
            except OSError:
                seed = ""
        try:
            path.write_text(seed, encoding="utf-8")
        except Exception:
            return seed
        return seed
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return SEED_PITCH if kind == "interview" else ""


def save_base_pitch(text: str, kind: str = "interview") -> None:
    """Save plain text and keep the styled HTML in sync when it exists."""
    kind = normalize_kind(kind)
    text = text or ""
    _write_md(text, kind)
    if has_html(kind) or (
            kind == "interview" and DEFAULT_PITCH_HTML_SOURCE.is_file()
    ) or (kind == "recruiter" and SEED_RECRUITER_HTML.is_file()):
        save_html(html_from_plain(text, kind=kind), kind=kind, sync_text=False)


def _read_html_raw(kind: str = "interview") -> str:
    path = pitch_html_path(kind)
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def load_html(kind: str = "interview") -> str:
    """Return the styled pitch HTML (empty string if missing)."""
    ensure_html(kind)
    return _read_html_raw(kind)


def save_html(html: str, *, kind: str = "interview", sync_text: bool = True) -> None:
    """Persist the styled HTML; optionally sync plain text into the .md mirror."""
    kind = normalize_kind(kind)
    html = html or ""
    dest = pitch_html_path(kind)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(html, encoding="utf-8")
    if sync_text and html.strip():
        plain = html_to_plain_text(html)
        if plain.strip():
            _write_md(plain, kind)


def html_to_plain_text(html: str) -> str:
    """Visible text from the pitch HTML — for TTS, AI, and simple text edit."""
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html or "", "html.parser")
        for tag in soup(["script", "style"]):
            tag.decompose()
        root = soup.find(class_="wrap") or soup.body or soup
        # Flatten each block to one line first. Otherwise get_text("\\n", strip=True)
        # splits NavigableStrings inside <h3><span class="num">1</span>title</h3>
        # into "1\\ntitle".
        # Flatten leaf blocks only — never <header>/<section> wrappers, or
        # title+subtitle collapse into one line.
        for tag in root.find_all(["h1", "h2", "h3", "h4", "p", "li", "footer"]):
            flat = tag.get_text(" ", strip=True)
            tag.clear()
            tag.append(flat)
        text = root.get_text("\n", strip=True)
    except Exception:
        text = re.sub(r"(?is)<script.*?>.*?</script>", " ", html or "")
        text = re.sub(r"(?is)<style.*?>.*?</style>", " ", text)
        text = re.sub(r"(?s)<[^>]+>", "\n", text)
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


_DEFAULT_STYLE = """
  :root{
    --bg:#0f172a; --card:#ffffff; --ink:#1f2937; --muted:#6b7280;
    --accent:#1a5276; --accent2:#2563eb; --line:#e5e7eb; --chip:#eef2ff;
  }
  *{box-sizing:border-box;}
  body{
    margin:0; background:linear-gradient(160deg,#eef2f7,#dbe4f0);
    font-family:'Segoe UI','Arial',sans-serif; color:var(--ink);
    direction:rtl; text-align:right; line-height:1.7; padding:32px 16px;
  }
  .wrap{max-width:860px; margin:0 auto;}
  header{
    background:linear-gradient(135deg,#1a5276,#2563eb); color:#fff;
    border-radius:18px; padding:28px 32px; box-shadow:0 12px 30px rgba(30,64,120,.25);
  }
  header h1{margin:0 0 6px; font-size:26px;}
  header p{margin:0; opacity:.92; font-size:15px;}
  .card{
    background:var(--card); border-radius:16px; padding:22px 28px; margin-top:20px;
    box-shadow:0 6px 20px rgba(15,23,42,.06); border:1px solid var(--line);
  }
  h2{
    font-size:21px; color:var(--accent); margin:4px 0 14px;
    border-bottom:2px solid var(--line); padding-bottom:8px;
  }
  h3{font-size:17px; color:var(--accent2); margin:20px 0 6px;}
  h3 .num{
    display:inline-flex; align-items:center; justify-content:center;
    width:26px; height:26px; border-radius:50%; background:var(--accent2);
    color:#fff; font-size:14px; margin-left:8px; vertical-align:middle;
  }
  p{margin:8px 0;}
  ul{margin:8px 0; padding-inline-start:22px;}
  li{margin:6px 0;}
  .tip{
    background:var(--chip); border-right:4px solid var(--accent2);
    padding:10px 14px; border-radius:8px; font-size:14px; color:#334155;
  }
  .lead{color:var(--muted); font-size:14px; margin-top:4px;}
  strong{color:#0f2f4a;}
  .quote{background:#f8fafc; border-radius:10px; padding:2px 16px; border:1px solid var(--line);}
  footer{text-align:center; color:var(--muted); font-size:13px; margin:26px 0 4px;}
  @media print{
    body{background:#fff; padding:0;}
    .card,header{box-shadow:none;}
  }
""".strip()


def _template_shell(existing_html: str = "") -> tuple[str, str]:
    """Return (title, style_css) from an existing HTML doc or defaults."""
    title = "הסקריפט המלא לראיון"
    style = _DEFAULT_STYLE
    if not existing_html.strip():
        src = DEFAULT_PITCH_HTML_SOURCE
        if src.is_file():
            try:
                existing_html = src.read_text(encoding="utf-8")
            except OSError:
                existing_html = ""
    if not existing_html.strip():
        return title, style
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(existing_html, "html.parser")
        if soup.title and soup.title.string:
            title = soup.title.string.strip() or title
        tag = soup.find("style")
        if tag and tag.string and tag.string.strip():
            style = tag.string.strip()
    except Exception:
        pass
    return title, style


def html_from_plain(text: str, *, template_html: str = "",
                    kind: str = "interview") -> str:
    """Build styled pitch HTML from plain text, reusing existing CSS when possible."""
    kind = normalize_kind(kind)
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    template = template_html or _read_html_raw(kind)
    if not template.strip() and kind == "recruiter" and SEED_RECRUITER_HTML.is_file():
        try:
            template = SEED_RECRUITER_HTML.read_text(encoding="utf-8")
        except OSError:
            template = ""
    title, style = _template_shell(template)
    if kind == "recruiter" and title == "הסקריפט המלא לראיון":
        title = "תסריט לגיוס"
    lines = text.split("\n") if text else []

    header_title = ""
    header_sub = ""
    body_start = 0
    if lines:
        first = lines[0].strip()
        # Only put short title-like lines in the blue <header>. Tailored AI
        # scripts often start with spoken prose ("נעים מאוד…") — that must
        # stay in white quote cards, not the banner.
        if _is_banner_title(first):
            header_title = first
            body_start = 1
            if len(lines) > 1:
                second = lines[1].strip()
                if second and _is_banner_subtitle(second):
                    header_sub = second
                    body_start = 2
            title = header_title

    # Skip blank lines after header
    while body_start < len(lines) and not lines[body_start].strip():
        body_start += 1

    parts: list[str] = []
    if header_title:
        parts.append("<header>")
        parts.append(f"<h1>{html_lib.escape(header_title)}</h1>")
        if header_sub:
            parts.append(f"<p>{html_lib.escape(header_sub)}</p>")
        parts.append("</header>")

    # Parse body into cards / stations / lists / quotes
    i = body_start
    current_card: list[str] = []
    current_h2 = ""
    quote_paras: list[str] = []
    list_items: list[str] = []

    def flush_quote() -> None:
        nonlocal quote_paras
        if not quote_paras:
            return
        current_card.append('<div class="quote">')
        for p in quote_paras:
            current_card.append(f"<p>{html_lib.escape(p)}</p>")
        current_card.append("</div>")
        quote_paras = []

    def flush_list() -> None:
        nonlocal list_items
        if not list_items:
            return
        current_card.append("<ul>")
        for item in list_items:
            current_card.append(f"<li>{item}</li>")
        current_card.append("</ul>")
        list_items = []

    def flush_card() -> None:
        nonlocal current_card, current_h2
        flush_quote()
        flush_list()
        if not current_card and not current_h2:
            return
        parts.append('<section class="card">')
        if current_h2:
            parts.append(f"<h2>{html_lib.escape(current_h2)}</h2>")
        parts.extend(current_card)
        parts.append("</section>")
        current_card = []
        current_h2 = ""

    def rich_inline(s: str) -> str:
        """Escape, then lightly mark **bold** / leading labels before ':'."""
        esc = html_lib.escape(s)
        esc = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", esc)
        # "label: rest" at start of bullet → bold label
        esc = re.sub(
            r"^([^:<]{1,40}):\s+",
            lambda m: f"<strong>{m.group(1)}:</strong> ",
            esc,
            count=1,
        )
        return esc

    while i < len(lines):
        raw = lines[i]
        ln = raw.strip()
        i += 1
        if not ln:
            flush_quote()
            flush_list()
            continue

        if _SECTION_RE.match(ln):
            flush_card()
            current_h2 = ln
            continue

        tip_m = _TIP_RE.match(ln)
        if tip_m:
            flush_quote()
            flush_list()
            body = tip_m.group(2).strip()
            current_card.append(
                f'<p class="tip"><strong>{html_lib.escape(tip_m.group(1))}:</strong> '
                f"{html_lib.escape(body)}</p>"
            )
            continue

        st_m = _STATION_RE.match(ln)
        if st_m:
            flush_quote()
            num, rest = st_m.group(1), st_m.group(2).strip()
            # Summary bullet already on one line: "1. הפתיח: long body…"
            if ":" in rest:
                before, after = rest.split(":", 1)
                if after.strip() and (
                    len(after.strip()) > 15
                    or ("–" not in before and "—" not in before)
                ):
                    # Keep as a list item unless "before" looks like a long station title
                    if "–" not in before and "—" not in before and len(before) < 40:
                        list_items.append(
                            f"<strong>{html_lib.escape(num)}. "
                            f"{html_lib.escape(before.strip())}:</strong> "
                            f"{html_lib.escape(after.strip())}"
                        )
                        continue
            label = rest.rstrip(":")
            is_heading = (
                "–" in label or "—" in label
                or any(k in label for k in (
                    "הפתיח", "הביזנס", "החדשנות", "הילד", "הסגירה",
                    "סיכום", "התשוקה", "העבודה", "הבוסטר",
                    "מי אני", "המיומנויות", "הערך",
                    "Opening", "Day-to-day", "Impact", "Passion", "Summary",
                ))
            )
            if is_heading:
                flush_list()
                current_card.append(
                    f'<h3><span class="num">{html_lib.escape(num)}</span>'
                    f"{html_lib.escape(label)}</h3>"
                )
            else:
                # Compact "1. הפתיח:" with body on the next line
                if i < len(lines) and lines[i].strip() and not _SECTION_RE.match(
                        lines[i].strip()):
                    nxt = lines[i].strip()
                    if not _STATION_RE.match(nxt) and not _BULLET_RE.match(nxt):
                        i += 1
                        list_items.append(
                            f"<strong>{html_lib.escape(num)}. "
                            f"{html_lib.escape(label)}:</strong> "
                            f"{html_lib.escape(nxt)}"
                        )
                        continue
                list_items.append(
                    f"<strong>{html_lib.escape(num)}. "
                    f"{html_lib.escape(label)}</strong>"
                )
            continue

        b_m = _BULLET_RE.match(ln)
        if b_m:
            flush_quote()
            list_items.append(rich_inline(b_m.group(1)))
            continue

        # Lead / muted short intro lines
        if (ln.startswith("אל תשנן") or ln.lower().startswith("don't memor")
                or ln.startswith("Tip:") or len(ln) < 90 and current_h2 and not quote_paras
                and not current_card):
            flush_list()
            current_card.append(f'<p class="lead">{html_lib.escape(ln)}</p>')
            continue

        # Spoken script paragraphs → quote block
        flush_list()
        quote_paras.append(ln.strip('"').strip("'").strip("״").strip("״"))

    flush_card()

    if not parts:
        parts.append('<section class="card"><p class="lead">(empty pitch)</p></section>')

    body = "\n  ".join(parts)
    return f"""<!DOCTYPE html>
<html lang="he" dir="rtl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{html_lib.escape(title)}</title>
<style>
{style}
</style>
</head>
<body>
<div class="wrap">
  {body}
</div>
</body>
</html>
"""


def ensure_html(kind: str | None = None) -> Path | None:
    """Ensure profile pitch HTML exists for ``kind`` (or both kinds).

    When HTML exists, the ``.md`` mirror is always refreshed from it (HTML wins).
    """
    if kind is None:
        ensure_html("interview")
        return ensure_html("recruiter")

    kind = normalize_kind(kind)
    dest = pitch_html_path(kind)
    if not dest.is_file():
        src = (DEFAULT_PITCH_HTML_SOURCE if kind == "interview"
               else SEED_RECRUITER_HTML)
        if src.is_file():
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
            except OSError:
                return None
        else:
            return None

    try:
        html = dest.read_text(encoding="utf-8")
        plain = html_to_plain_text(html)
        if plain.strip():
            _write_md(plain, kind)
    except OSError:
        pass
    return dest


# --------------------------------------------------------------------------- #
# AI-revision draft

def _draft_path(kind: str = "interview"):
    kind = normalize_kind(kind)
    name = ("pitch_recruiter_draft.md" if kind == "recruiter"
            else "pitch_draft.md")
    return pitch_md_path(kind).with_name(name)


def load_draft(kind: str = "interview") -> str:
    try:
        return _draft_path(kind).read_text(encoding="utf-8")
    except OSError:
        return ""


def save_draft(text: str, kind: str = "interview") -> None:
    _draft_path(kind).write_text(text or "", encoding="utf-8")


def clear_draft(kind: str = "interview") -> None:
    _draft_path(kind).unlink(missing_ok=True)


def has_draft(kind: str = "interview") -> bool:
    return bool(load_draft(kind).strip())


# --------------------------------------------------------------------------- #
# Word-level highlight inside styled pitch HTML (Before / After compare)

_HL_CSS = """
.pitch-hl-del{background:rgba(251,113,133,.28);color:#9f1239;text-decoration:line-through;
  text-decoration-color:rgba(190,18,60,.7);border-radius:3px;padding:0 1px}
.pitch-hl-ins{background:rgba(52,211,153,.28);color:#065f46;text-decoration:none;
  border-radius:3px;padding:0 1px}
""".strip()


def _word_flags(old: str, new: str) -> tuple[list[bool], list[bool]]:
    """Per non-whitespace-word flags: deleted in old, inserted in new."""
    import difflib
    a = re.findall(r"\S+", old or "")
    b = re.findall(r"\S+", new or "")
    del_flags = [False] * len(a)
    ins_flags = [False] * len(b)
    sm = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op in ("delete", "replace"):
            for i in range(i1, i2):
                del_flags[i] = True
        if op in ("insert", "replace"):
            for j in range(j1, j2):
                ins_flags[j] = True
    return del_flags, ins_flags


def highlight_html_diff(html: str, *, old: str, new: str,
                        side: str = "after") -> str:
    """Wrap changed words in styled pitch HTML.

    ``side="before"`` → red strikethrough on words removed vs ``new``.
    ``side="after"``  → green highlight on words added vs ``old``.
    """
    try:
        from bs4 import BeautifulSoup, NavigableString
    except ImportError:
        return html

    side = (side or "after").strip().lower()
    del_flags, ins_flags = _word_flags(old, new)
    flags = del_flags if side == "before" else ins_flags
    soup = BeautifulSoup(html or "", "html.parser")
    if not any(flags):
        _inject_hl_css(soup)
        return str(soup)

    root = soup.find(class_="wrap") or soup.body or soup
    tag_name = "del" if side == "before" else "ins"
    css_class = "pitch-hl-del" if side == "before" else "pitch-hl-ins"

    word_i = 0
    text_nodes = [
        n for n in root.descendants
        if isinstance(n, NavigableString)
        and n.parent
        and getattr(n.parent, "name", None) not in ("script", "style")
        and str(n).strip()
    ]

    for node in text_nodes:
        text = str(node)
        parts: list = []
        last = 0
        for m in re.finditer(r"\S+", text):
            if word_i >= len(flags):
                break
            if m.start() > last:
                parts.append(text[last:m.start()])
            word = m.group(0)
            if flags[word_i]:
                wrap = soup.new_tag(tag_name)
                wrap["class"] = [css_class]
                wrap.string = word
                parts.append(wrap)
            else:
                parts.append(word)
            word_i += 1
            last = m.end()
        if last < len(text):
            parts.append(text[last:])
        if not parts:
            continue
        for part in parts:
            node.insert_before(part)
        node.extract()

    _inject_hl_css(soup)
    return str(soup)


def _inject_hl_css(soup) -> None:
    style = soup.find("style")
    if style is None:
        head = soup.head
        if head is None:
            return
        style = soup.new_tag("style")
        head.append(style)
    existing = style.string or ""
    if "pitch-hl-del" not in existing:
        style.string = existing.rstrip() + "\n" + _HL_CSS + "\n"
