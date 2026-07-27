"""Resume library — store each unique CV once, link applications to it.

Files live under ``data/profiles/<active>/resumes/``. Deduping is by SHA-256
of file bytes: selecting the same resume again just reuses the existing row.
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
from pathlib import Path
from typing import Any

from . import config
from .db import get_connection, now_iso
from .resume import SUPPORTED_RESUME_EXTS

_HASH_PREFIX = 12  # short hash in stored filenames

# Visual colour chips for the library cards (key → CSS-friendly name).
RESUME_COLORS: list[tuple[str, str]] = [
    ("blue", "Blue"),
    ("teal", "Teal"),
    ("violet", "Violet"),
    ("amber", "Amber"),
    ("rose", "Rose"),
    ("slate", "Slate"),
    ("green", "Green"),
    ("orange", "Orange"),
]
_COLOR_KEYS = {c for c, _ in RESUME_COLORS}


def _dir() -> Path:
    d = config.PROFILE_DIR / "resumes"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _draft_dir() -> Path:
    d = _dir() / "drafts"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe_name(name: str) -> str:
    base = Path(name or "resume").name
    base = re.sub(r"[^\w.\-]+", "_", base, flags=re.UNICODE).strip("._") or "resume"
    return base[:120]


def _label_from_name(name: str) -> str:
    stem = Path(name or "resume").stem.replace("_", " ").strip()
    return stem[:80] or "Resume"


def _normalize_color(color: str | None) -> str:
    c = (color or "blue").strip().lower()
    return c if c in _COLOR_KEYS else "blue"


def _distinct_label(desired: str, original_name: str, content_hash: str) -> str:
    """Keep human label; if another resume already uses it, append a version hint.

    Identity is always ``content_hash`` — this only makes the UI readable when
    two different files share the same display name.
    """
    base = (desired or _label_from_name(original_name)).strip()[:80] or "Resume"
    short = content_hash[:8]
    with get_connection() as conn:
        clash = conn.execute(
            """SELECT id FROM resumes
                WHERE lower(label)=lower(?) AND content_hash!=?""",
            (base, content_hash),
        ).fetchone()
        name_clash = conn.execute(
            """SELECT id FROM resumes
                WHERE lower(original_name)=lower(?) AND content_hash!=?""",
            (Path(original_name).name.lower(), content_hash),
        ).fetchone() if original_name else None
    if not clash and not name_clash:
        return base
    suffix = f" (v·{short})"
    return base[: max(1, 80 - len(suffix))] + suffix


def list_resumes() -> list[sqlite3.Row]:
    with get_connection() as conn:
        return list(conn.execute(
            """SELECT * FROM resumes
                ORDER BY is_default DESC, created_at DESC, id DESC"""
        ).fetchall())


def get(resume_id: int) -> sqlite3.Row | None:
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM resumes WHERE id=?", (resume_id,)
        ).fetchone()


def get_default() -> sqlite3.Row | None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM resumes WHERE is_default=1 ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row:
            return row
        return conn.execute(
            "SELECT * FROM resumes ORDER BY created_at DESC, id DESC LIMIT 1"
        ).fetchone()


def path_for(row: sqlite3.Row | dict[str, Any]) -> Path:
    return _dir() / row["filename"]


def is_html(row: sqlite3.Row | dict[str, Any]) -> bool:
    name = (row["original_name"] or row["filename"] or "").lower()
    return Path(name).suffix in (".html", ".htm")


def find_by_hash(content_hash: str) -> sqlite3.Row | None:
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM resumes WHERE content_hash=?", (content_hash,)
        ).fetchone()


def _insert(*, label: str, content_hash: str, filename: str,
            original_name: str, source_path: str, size: int,
            color: str = "blue", is_default: int = 0) -> int:
    ts = now_iso()
    with get_connection() as conn:
        cur = conn.execute(
            """INSERT INTO resumes
                 (label, content_hash, filename, original_name, source_path,
                  bytes, color, is_default, created_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (label, content_hash, filename, original_name, source_path,
             size, _normalize_color(color), int(is_default), ts),
        )
        return int(cur.lastrowid)


def ensure_from_bytes(
    data: bytes,
    *,
    original_name: str,
    label: str = "",
    source_path: str = "",
    color: str = "blue",
    make_default: bool = False,
) -> tuple[int, bool]:
    """Store bytes if new; return ``(resume_id, created)``.

    Dedupes by **file content** (SHA-256), not by filename/label.
    """
    if not data:
        raise ValueError("Empty resume file")
    ext = Path(original_name or "resume.bin").suffix.lower()
    if ext and ext not in SUPPORTED_RESUME_EXTS:
        raise ValueError(
            f"Unsupported resume type {ext} "
            f"(use {', '.join(sorted(SUPPORTED_RESUME_EXTS))})"
        )
    content_hash = _sha256(data)
    existing = find_by_hash(content_hash)
    if existing:
        rid = int(existing["id"])
        if make_default:
            set_default(rid)
        return rid, False

    safe = _safe_name(original_name)
    if not Path(safe).suffix and ext:
        safe = f"{safe}{ext}"
    stored = f"{content_hash[:_HASH_PREFIX]}_{safe}"
    dest = _dir() / stored
    dest.write_bytes(data)
    rid = _insert(
        label=_distinct_label(
            (label or "").strip(), original_name, content_hash),
        content_hash=content_hash,
        filename=stored,
        original_name=Path(original_name).name,
        source_path=(source_path or "")[:500],
        size=len(data),
        color=color,
        is_default=0,
    )
    if make_default:
        set_default(rid)
    return rid, True


def ensure_from_path(path: Path | str, *, label: str = "",
                     color: str = "blue",
                     make_default: bool = False) -> tuple[int, bool]:
    """Import a filesystem resume; dedupe by content hash."""
    p = Path(path).expanduser()
    if not p.is_file():
        raise FileNotFoundError(f"Resume not found: {p}")
    data = p.read_bytes()
    return ensure_from_bytes(
        data,
        original_name=p.name,
        label=label or _label_from_name(p.name),
        source_path=str(p.resolve()),
        color=color,
        make_default=make_default,
    )


def ensure_from_html(html: str, *, label: str, color: str = "violet",
                     original_name: str = "built_resume.html",
                     make_default: bool = False) -> tuple[int, bool]:
    """Add an HTML document (e.g. Resume Builder output) to the library."""
    data = (html or "").encode("utf-8")
    return ensure_from_bytes(
        data,
        original_name=original_name,
        label=label,
        color=color,
        make_default=make_default,
    )


def ensure_defaults() -> list[sqlite3.Row]:
    """Register Settings resume + built resume into the library (if present)."""
    candidates: list[tuple[Path, str]] = []
    rp = Path(config.RESUME_PATH) if config.RESUME_PATH else None
    if rp and rp.is_file():
        candidates.append((rp, f"Default — {rp.name}"))
    built = config.BUILT_RESUME_PATH
    if built.is_file():
        candidates.append((built, "Built resume (Resume Builder)"))
    for path, label in candidates:
        try:
            ensure_from_path(path, label=label)
        except OSError:
            pass
    with get_connection() as conn:
        has_def = conn.execute(
            "SELECT 1 FROM resumes WHERE is_default=1 LIMIT 1"
        ).fetchone()
    if not has_def:
        rows = list_resumes()
        if rows:
            pick = next(
                (r for r in rows if (r["label"] or "").startswith("Default")),
                rows[0],
            )
            set_default(int(pick["id"]), update_settings_path=False)
    return list_resumes()


def set_label(resume_id: int, label: str) -> None:
    label = (label or "").strip()[:80]
    if not label:
        return
    with get_connection() as conn:
        conn.execute("UPDATE resumes SET label=? WHERE id=?", (label, resume_id))


def set_color(resume_id: int, color: str) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE resumes SET color=? WHERE id=?",
            (_normalize_color(color), resume_id),
        )


def set_default(resume_id: int, *, update_settings_path: bool = True) -> None:
    """Mark one library resume as default (clears others).

    Optionally points Settings ``RESUME_PATH`` at its stored file so AI features
    use it.
    """
    row = get(resume_id)
    if not row:
        raise ValueError(f"Unknown resume id {resume_id}")
    with get_connection() as conn:
        conn.execute("UPDATE resumes SET is_default=0")
        conn.execute(
            "UPDATE resumes SET is_default=1 WHERE id=?", (resume_id,))
    if update_settings_path:
        path = path_for(row)
        if path.is_file():
            try:
                config.update_env_file({"RESUME_PATH": str(path.resolve())})
                config.reload()
            except OSError:
                pass


def update_meta(resume_id: int, *, label: str | None = None,
                color: str | None = None) -> None:
    if label is not None:
        set_label(resume_id, label)
    if color is not None:
        set_color(resume_id, color)


def save_html(resume_id: int, html: str) -> None:
    """Overwrite an HTML resume's bytes (updates content hash in place)."""
    row = get(resume_id)
    if not row:
        raise ValueError(f"Unknown resume id {resume_id}")
    if not is_html(row):
        raise ValueError("Only HTML resumes can be saved as HTML")
    data = (html or "").encode("utf-8")
    content_hash = _sha256(data)
    other = find_by_hash(content_hash)
    if other and int(other["id"]) != resume_id:
        raise ValueError(
            "That content already exists as another library resume "
            f"(#{other['id']}: {other['label']}).")
    path = path_for(row)
    path.write_bytes(data)
    with get_connection() as conn:
        conn.execute(
            "UPDATE resumes SET content_hash=?, bytes=? WHERE id=?",
            (content_hash, len(data), resume_id),
        )


def draft_path(resume_id: int) -> Path:
    return _draft_dir() / f"{resume_id}.html"


def save_draft(resume_id: int, html: str) -> None:
    draft_path(resume_id).write_text(html or "", encoding="utf-8")


def load_draft(resume_id: int) -> str:
    p = draft_path(resume_id)
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        return ""


def clear_draft(resume_id: int) -> None:
    draft_path(resume_id).unlink(missing_ok=True)


def has_draft(resume_id: int) -> bool:
    return bool(load_draft(resume_id).strip())


def apply_draft(resume_id: int) -> None:
    draft = load_draft(resume_id)
    if not draft.strip():
        raise ValueError("No pending AI revision")
    save_html(resume_id, draft)
    clear_draft(resume_id)


def attach_to_application(
    app_id: int,
    resume_id: int | None,
    *,
    note: str = "",
) -> None:
    """Link (or clear) which resume was sent for this application."""
    with get_connection() as conn:
        prev = conn.execute(
            "SELECT resume_id FROM applications WHERE id=?", (app_id,)
        ).fetchone()
        if not prev:
            raise ValueError(f"Unknown application {app_id}")
        old_id = prev["resume_id"]
        label = ""
        if resume_id:
            row = conn.execute(
                "SELECT label FROM resumes WHERE id=?", (resume_id,)
            ).fetchone()
            if not row:
                raise ValueError(f"Unknown resume id {resume_id}")
            label = row["label"] or ""
        ts = now_iso()
        if old_id and old_id != resume_id:
            conn.execute(
                """INSERT INTO application_resume_history
                     (application_id, resume_id, note, attached_at)
                   VALUES (?,?,?,?)""",
                (app_id, old_id,
                 (note or "replaced").strip()[:200], ts),
            )
        conn.execute(
            """UPDATE applications
                  SET resume_id=?, resume_version=?, updated_at=?
                WHERE id=?""",
            (resume_id, label, ts, app_id),
        )


def history_for(app_id: int) -> list[sqlite3.Row]:
    """Previous resumes used for this application (newest first)."""
    with get_connection() as conn:
        return list(conn.execute(
            """SELECT h.id, h.note, h.attached_at, h.resume_id,
                      r.label, r.original_name, r.content_hash, r.created_at,
                      r.color
                 FROM application_resume_history h
                 JOIN resumes r ON r.id = h.resume_id
                WHERE h.application_id=?
                ORDER BY h.attached_at DESC, h.id DESC""",
            (app_id,),
        ).fetchall())


def for_application(app_id: int) -> sqlite3.Row | None:
    with get_connection() as conn:
        return conn.execute(
            """SELECT r.* FROM resumes r
                 JOIN applications a ON a.resume_id = r.id
                WHERE a.id=?""",
            (app_id,),
        ).fetchone()


def usage_counts() -> dict[int, int]:
    """How many applications reference each resume (current + history)."""
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT resume_id, COUNT(*) AS n FROM (
                  SELECT resume_id FROM applications WHERE resume_id IS NOT NULL
                  UNION ALL
                  SELECT resume_id FROM application_resume_history
                )
                GROUP BY resume_id"""
        ).fetchall()
    return {int(r["resume_id"]): int(r["n"]) for r in rows}


def resolve_selection(
    *,
    resume_id: str | int | None = None,
    upload=None,
    upload_label: str = "",
    path_text: str = "",
    color: str = "blue",
) -> int | None:
    """Resolve paste/detail form fields to a resume id (or None).

    Priority: new upload → new path → existing id.
    """
    if upload is not None and getattr(upload, "filename", None):
        raw = upload.read()
        if raw:
            rid, _ = ensure_from_bytes(
                raw,
                original_name=upload.filename,
                label=upload_label,
                color=color,
            )
            return rid
    path_text = (path_text or "").strip()
    if path_text:
        rid, _ = ensure_from_path(path_text, label=upload_label, color=color)
        return rid
    if resume_id in (None, "", "0", 0):
        return None
    rid = int(resume_id)
    if not get(rid):
        raise ValueError(f"Unknown resume id {rid}")
    return rid
