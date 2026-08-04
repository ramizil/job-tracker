"""Rejection evidence files — PDFs, screenshots, feedback notes per application.

Stored under ``data/profiles/<active>/rejection_files/<app_id>/``.
"""
from __future__ import annotations

import mimetypes
import re
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from . import config
from .db import get_connection, now_iso

SUPPORTED_EXTS = {
    ".pdf", ".txt", ".md", ".markdown", ".html", ".htm",
    ".docx", ".png", ".jpg", ".jpeg", ".webp", ".gif",
}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
TEXT_EXTS = {".pdf", ".txt", ".md", ".markdown", ".html", ".htm", ".docx"}


def _dir(app_id: int) -> Path:
    d = Path(config.PROFILE_DIR) / "rejection_files" / str(int(app_id))
    d.mkdir(parents=True, exist_ok=True)
    return d


def _safe_name(name: str) -> str:
    base = Path(name or "file").name
    base = re.sub(r"[^\w.\-]+", "_", base, flags=re.UNICODE).strip("._") or "file"
    return base[:120]


def path_for(row: sqlite3.Row | dict[str, Any]) -> Path:
    app_id = int(row["application_id"])
    return _dir(app_id) / row["stored_name"]


def list_for(app_id: int) -> list[sqlite3.Row]:
    with get_connection() as conn:
        return list(conn.execute(
            """SELECT * FROM rejection_files
                WHERE application_id=?
                ORDER BY created_at DESC, id DESC""",
            (int(app_id),),
        ).fetchall())


def get(file_id: int) -> sqlite3.Row | None:
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM rejection_files WHERE id=?", (int(file_id),)
        ).fetchone()


def counts_for(app_ids: list[int]) -> dict[int, int]:
    ids = sorted({int(i) for i in app_ids if i})
    if not ids:
        return {}
    placeholders = ",".join("?" * len(ids))
    with get_connection() as conn:
        rows = conn.execute(
            f"""SELECT application_id, COUNT(*) AS n FROM rejection_files
                 WHERE application_id IN ({placeholders})
                 GROUP BY application_id""",
            ids,
        ).fetchall()
    return {int(r["application_id"]): int(r["n"]) for r in rows}


def add(app_id: int, *, data: bytes, original_name: str,
        note: str = "", mime: str = "") -> int:
    """Store a file and return its id. Raises ValueError on bad input."""
    if not data:
        raise ValueError("Empty file.")
    original_name = _safe_name(original_name)
    ext = Path(original_name).suffix.lower()
    if ext not in SUPPORTED_EXTS:
        raise ValueError(
            f"Unsupported file type “{ext or 'none'}”. "
            f"Use: {', '.join(sorted(SUPPORTED_EXTS))}")
    mime = (mime or mimetypes.guess_type(original_name)[0] or "").strip()
    stored = f"{uuid.uuid4().hex[:12]}_{original_name}"
    dest = _dir(app_id) / stored
    dest.write_bytes(data)
    with get_connection() as conn:
        cur = conn.execute(
            """INSERT INTO rejection_files
                 (application_id, original_name, stored_name, mime, note, created_at)
               VALUES (?,?,?,?,?,?)""",
            (int(app_id), original_name, stored, mime,
             (note or "").strip()[:500], now_iso()),
        )
        return int(cur.lastrowid)


def delete(file_id: int) -> bool:
    row = get(file_id)
    if not row:
        return False
    path = path_for(row)
    with get_connection() as conn:
        conn.execute("DELETE FROM rejection_files WHERE id=?", (int(file_id),))
    try:
        if path.is_file():
            path.unlink()
    except OSError:
        pass
    return True


def is_image(row: sqlite3.Row | dict[str, Any]) -> bool:
    name = (row["original_name"] or row["stored_name"] or "").lower()
    return Path(name).suffix.lower() in IMAGE_EXTS


def text_excerpt(app_id: int, *, max_chars: int = 12000) -> str:
    """Concatenate extractable text from attachments for AI prompts."""
    from . import resume as resume_mod

    parts: list[str] = []
    used = 0
    for row in list_for(app_id):
        if used >= max_chars:
            break
        path = path_for(row)
        label = row["original_name"] or path.name
        note = (row["note"] or "").strip()
        header = f"--- Attachment: {label}"
        if note:
            header += f" (note: {note})"
        header += " ---"
        ext = Path(label).suffix.lower()
        if ext in IMAGE_EXTS:
            chunk = f"{header}\n[Image file attached — content not OCR’d in text mode.]"
        elif ext in TEXT_EXTS and path.is_file():
            try:
                text = resume_mod.extract_text(path).strip()
            except Exception as exc:
                text = f"[Could not extract text: {exc}]"
            if not text:
                text = "[No extractable text.]"
            chunk = f"{header}\n{text}"
        else:
            chunk = f"{header}\n[Binary / unsupported for text extraction.]"
        remain = max_chars - used
        if len(chunk) > remain:
            chunk = chunk[: remain - 20] + "\n…[truncated]"
        parts.append(chunk)
        used += len(chunk)
    return "\n\n".join(parts).strip()


def image_parts_for_gemini(app_id: int, *, max_images: int = 4
                           ) -> list[tuple[bytes, str]]:
    """Return (bytes, mime) for image attachments (Gemini multimodal)."""
    out: list[tuple[bytes, str]] = []
    for row in list_for(app_id):
        if len(out) >= max_images:
            break
        if not is_image(row):
            continue
        path = path_for(row)
        if not path.is_file():
            continue
        mime = (row["mime"] or mimetypes.guess_type(path.name)[0]
                or "image/png")
        try:
            out.append((path.read_bytes(), mime.split(";", 1)[0].strip()))
        except OSError:
            continue
    return out
