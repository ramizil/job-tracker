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


def _dir() -> Path:
    d = config.PROFILE_DIR / "resumes"
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


def _distinct_label(desired: str, original_name: str, content_hash: str) -> str:
    """Keep human label; if another resume already uses it, append a version hint.

    Identity is always ``content_hash`` — this only makes the dropdown readable
    when two different files share the same display name.
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
    # e.g. "Senior QA Engineer (v·a1b2c3d4)" — stable, unique, not an overwrite
    suffix = f" (v·{short})"
    out = (base[: max(1, 80 - len(suffix))] + suffix)
    return out


def list_resumes() -> list[sqlite3.Row]:
    with get_connection() as conn:
        return list(conn.execute(
            "SELECT * FROM resumes ORDER BY created_at DESC, id DESC"
        ).fetchall())


def get(resume_id: int) -> sqlite3.Row | None:
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM resumes WHERE id=?", (resume_id,)
        ).fetchone()


def path_for(row: sqlite3.Row | dict[str, Any]) -> Path:
    return _dir() / row["filename"]


def find_by_hash(content_hash: str) -> sqlite3.Row | None:
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM resumes WHERE content_hash=?", (content_hash,)
        ).fetchone()


def _insert(*, label: str, content_hash: str, filename: str,
            original_name: str, source_path: str, size: int) -> int:
    ts = now_iso()
    with get_connection() as conn:
        cur = conn.execute(
            """INSERT INTO resumes
                 (label, content_hash, filename, original_name, source_path,
                  bytes, created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (label, content_hash, filename, original_name, source_path,
             size, ts),
        )
        return int(cur.lastrowid)


def ensure_from_bytes(
    data: bytes,
    *,
    original_name: str,
    label: str = "",
    source_path: str = "",
) -> tuple[int, bool]:
    """Store bytes if new; return ``(resume_id, created)``.

    Dedupes by **file content** (SHA-256), not by filename/label. Uploading
    ``CV.pdf`` twice with different bytes keeps both rows; identical bytes
    reuse the existing row and never overwrite it.
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
        return int(existing["id"]), False

    safe = _safe_name(original_name)
    if not Path(safe).suffix and ext:
        safe = f"{safe}{ext}"
    # Hash prefix in the stored filename → same original name never clobbers
    # an older version on disk.
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
    )
    return rid, True


def ensure_from_path(path: Path | str, *, label: str = "") -> tuple[int, bool]:
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
    return list_resumes()


def set_label(resume_id: int, label: str) -> None:
    label = (label or "").strip()[:80]
    if not label:
        return
    with get_connection() as conn:
        conn.execute("UPDATE resumes SET label=? WHERE id=?", (label, resume_id))


def attach_to_application(
    app_id: int,
    resume_id: int | None,
    *,
    note: str = "",
) -> None:
    """Link (or clear) which resume was sent for this application.

    When replacing an existing resume with a different one, the previous link
    is archived in ``application_resume_history`` so older CVs stay available
    for insights (e.g. after a reapply).
    """
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
        # Archive previous resume before overwriting the current link.
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
                      r.label, r.original_name, r.content_hash, r.created_at
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
            )
            return rid
    path_text = (path_text or "").strip()
    if path_text:
        rid, _ = ensure_from_path(path_text, label=upload_label)
        return rid
    if resume_id in (None, "", "0", 0):
        return None
    rid = int(resume_id)
    if not get(rid):
        raise ValueError(f"Unknown resume id {rid}")
    return rid
