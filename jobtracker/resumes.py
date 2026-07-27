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


def is_pdf(row: sqlite3.Row | dict[str, Any]) -> bool:
    name = (row["original_name"] or row["filename"] or "").lower()
    return Path(name).suffix == ".pdf"


def _stem_key(row: sqlite3.Row | dict[str, Any]) -> str:
    name = row["original_name"] or row["filename"] or ""
    stem = Path(name).stem.lower()
    # Strip common version suffixes like " (v·abcdef12)" from labels used as names
    stem = re.sub(r"\s*\(v[·\.]?[0-9a-f]{6,}\)\s*$", "", stem, flags=re.I)
    return re.sub(r"[_\s\-]+", "", stem)


def _norm_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip().lower()[:8000]


def extracted_text(row: sqlite3.Row | dict[str, Any]) -> str:
    """Plain text of a library resume (for twin matching)."""
    from . import resume as resume_mod
    path = path_for(row)
    if not path.is_file():
        # Fall back to original source path if library file missing
        src = (row["source_path"] or "").strip()
        if src and Path(src).is_file():
            path = Path(src)
        else:
            return ""
    try:
        return resume_mod.extract_text(path)
    except Exception:
        return ""


def _text_ratio(a: str, b: str) -> float:
    import difflib
    na, nb = _norm_text(a), _norm_text(b)
    if not na or not nb:
        return 0.0
    return difflib.SequenceMatcher(a=na, b=nb, autojunk=False).ratio()


def _disk_twin_path(row: sqlite3.Row | dict[str, Any], *, want: str) -> Path | None:
    """Find a sibling HTML/PDF on disk next to source_path or library file."""
    want = want.lower()
    candidates: list[Path] = []
    for base in (row["source_path"], str(path_for(row))):
        if not base:
            continue
        p = Path(base)
        if p.suffix.lower() in (".html", ".htm", ".pdf"):
            candidates.append(p)
        if p.parent.is_dir():
            stem = p.stem
            if want == "html":
                for ext in (".html", ".htm"):
                    cand = p.parent / f"{stem}{ext}"
                    if cand.is_file():
                        return cand
            elif want == "pdf":
                cand = p.parent / f"{stem}.pdf"
                if cand.is_file():
                    return cand
    return None


def list_resume_groups() -> list[dict[str, Any]]:
    """Group HTML+PDF twins (same content) into one library card.

    Pairing is content-based so different CV versions stay separate even when
    they share a filename stem:
      • HTML + PDF with text similarity ≥ 0.88 → one group (viewer opens HTML)
      • Near-duplicate files with the same filename stem (similarity ≥ 0.95)
        join the same group
    Primary prefers HTML. Cards list both HTML and PDF paths when known.
    """
    rows = list_resumes()
    if not rows:
        return []

    by_id = {int(r["id"]): r for r in rows}
    texts: dict[int, str] = {i: extracted_text(r) for i, r in by_id.items()}
    unused = set(by_id)
    clusters: list[list[int]] = []

    def _ratio(a: int, b: int) -> float:
        return _text_ratio(texts.get(a, ""), texts.get(b, ""))

    htmls = sorted(
        (i for i in unused if is_html(by_id[i])),
        key=lambda i: (0 if by_id[i]["is_default"] else 1, -i),
    )
    for hid in htmls:
        if hid not in unused:
            continue
        members = [hid]
        unused.remove(hid)
        # Best PDF twin by text (same stem is a soft boost for ties).
        best_pid, best_score = None, 0.0
        for pid in list(unused):
            if not is_pdf(by_id[pid]):
                continue
            score = _ratio(hid, pid)
            if _stem_key(by_id[hid]) and _stem_key(by_id[hid]) == _stem_key(by_id[pid]):
                score = min(1.0, score + 0.02)
            if score > best_score:
                best_score, best_pid = score, pid
        if best_pid is not None and best_score >= 0.88:
            members.append(best_pid)
            unused.remove(best_pid)
        # Absorb near-duplicates that share the same filename stem
        changed = True
        while changed:
            changed = False
            for other in list(unused):
                o_stem = _stem_key(by_id[other])
                if not o_stem:
                    continue
                for m in members:
                    if _stem_key(by_id[m]) != o_stem:
                        continue
                    if _ratio(other, m) >= 0.95:
                        members.append(other)
                        unused.remove(other)
                        changed = True
                        break
        clusters.append(members)

    # Remaining rows: cluster near-duplicates with the same stem, else singleton
    while unused:
        seed = max(
            unused,
            key=lambda i: (1 if by_id[i]["is_default"] else 0, i),
        )
        members = [seed]
        unused.remove(seed)
        seed_stem = _stem_key(by_id[seed])
        if seed_stem:
            for other in list(unused):
                if _stem_key(by_id[other]) != seed_stem:
                    continue
                if max(_ratio(other, m) for m in members) >= 0.95:
                    members.append(other)
                    unused.remove(other)
        clusters.append(members)

    groups: list[dict[str, Any]] = []
    for members in clusters:
        member_rows = [by_id[i] for i in members]
        html_row = next((r for r in member_rows if is_html(r)), None)
        pdf_candidates = [r for r in member_rows if is_pdf(r)]
        pdf_row = None
        if pdf_candidates:
            pdf_row = next((r for r in pdf_candidates if r["is_default"]), None)
            if pdf_row is None:
                pdf_row = max(pdf_candidates, key=lambda r: int(r["id"]))
        primary = html_row or next(
            (r for r in member_rows if r["is_default"]), member_rows[0]
        )

        html_path = ""
        pdf_path = ""
        if html_row:
            html_path = html_row["source_path"] or str(path_for(html_row))
        else:
            twin = _disk_twin_path(primary, want="html")
            if twin:
                html_path = str(twin)
        if pdf_row:
            pdf_path = pdf_row["source_path"] or str(path_for(pdf_row))
        else:
            twin = _disk_twin_path(primary, want="pdf")
            if twin:
                pdf_path = str(twin)

        label = (primary["label"] or "").strip()
        if html_row and label.lower().startswith("default —"):
            label = (html_row["label"] or label).strip()
        if label.lower().startswith("default —"):
            for r in member_rows:
                cand = (r["label"] or "").strip()
                if cand and not cand.lower().startswith("default —"):
                    label = cand
                    break

        groups.append({
            "primary": primary,
            "primary_id": int(primary["id"]),
            "members": member_rows,
            "member_ids": [int(r["id"]) for r in member_rows],
            "html": html_row,
            "pdf": pdf_row,
            "html_path": html_path,
            "pdf_path": pdf_path,
            "html_name": Path(html_path).name if html_path else "",
            "pdf_name": Path(pdf_path).name if pdf_path else "",
            "label": label,
            "color": primary["color"] or "blue",
            "is_default": any(bool(r["is_default"]) for r in member_rows),
            "created_at": primary["created_at"],
            "content_hash": (html_row or primary)["content_hash"],
        })

    groups.sort(key=lambda g: (
        0 if g["is_default"] else 1,
        -(g["primary_id"]),
    ))
    return groups


def group_for(resume_id: int) -> dict[str, Any] | None:
    for g in list_resume_groups():
        if resume_id in g["member_ids"]:
            return g
    return None


def viewer_row(resume_id: int) -> sqlite3.Row | None:
    """Row to open in the viewer — prefer HTML twin in the same content group."""
    g = group_for(resume_id)
    if not g:
        return get(resume_id)
    if g["html"]:
        return g["html"]
    return g["primary"]


def find_by_hash(content_hash: str) -> sqlite3.Row | None:
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM resumes WHERE content_hash=?", (content_hash,)
        ).fetchone()


def text_from_bytes(data: bytes, original_name: str = "") -> str:
    """Extract plain text from in-memory resume bytes (temp file)."""
    import tempfile
    from . import resume as resume_mod
    ext = Path(original_name or "resume.bin").suffix.lower() or ".bin"
    if ext not in SUPPORTED_RESUME_EXTS:
        return ""
    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
        tmp.write(data)
        path = Path(tmp.name)
    try:
        return resume_mod.extract_text(path)
    except Exception:
        return ""
    finally:
        path.unlink(missing_ok=True)


def match_existing(
    data: bytes,
    *,
    original_name: str = "",
    similar_threshold: float = 0.88,
) -> dict[str, Any] | None:
    """Find a library resume with the same bytes or nearly the same text.

    Returns ``{kind, row, resume_id, score, group, label}`` or ``None``.
    ``kind`` is ``\"exact\"`` (SHA-256) or ``\"similar\"`` (text ratio).
    """
    if not data:
        return None
    content_hash = _sha256(data)
    exact = find_by_hash(content_hash)
    if exact:
        rid = int(exact["id"])
        g = group_for(rid)
        return {
            "kind": "exact",
            "row": exact,
            "resume_id": rid,
            "score": 1.0,
            "group": g,
            "label": (g["label"] if g else None)
                     or exact["label"] or exact["original_name"] or f"#{rid}",
        }

    incoming = text_from_bytes(data, original_name)
    if len(_norm_text(incoming)) < 80:
        return None

    incoming_stem = re.sub(
        r"[_\s\-]+", "", Path(original_name or "").stem.lower())
    incoming_is_html = Path(original_name or "").suffix.lower() in (".html", ".htm")
    incoming_is_pdf = Path(original_name or "").suffix.lower() == ".pdf"

    best: sqlite3.Row | None = None
    best_score = 0.0
    for row in list_resumes():
        score = _text_ratio(incoming, extracted_text(row))
        stem = _stem_key(row)
        if incoming_stem and stem and incoming_stem == stem:
            # Same basename stem (html↔pdf) — soft boost for twin detection.
            if (incoming_is_html and is_pdf(row)) or (incoming_is_pdf and is_html(row)):
                score = min(1.0, score + 0.02)
        if score > best_score:
            best_score, best = score, row

    if best is None or best_score < similar_threshold:
        return None
    rid = int(best["id"])
    g = group_for(rid)
    return {
        "kind": "similar",
        "row": best,
        "resume_id": rid,
        "score": best_score,
        "group": g,
        "label": (g["label"] if g else None)
                 or best["label"] or best["original_name"] or f"#{rid}",
    }


def preferred_match_id(match: dict[str, Any]) -> int:
    """Id to open / attach when reusing a match (HTML twin preferred)."""
    g = match.get("group")
    if g and g.get("html"):
        return int(g["html"]["id"])
    if g and g.get("primary_id"):
        return int(g["primary_id"])
    return int(match["resume_id"])


def _pending_dir() -> Path:
    d = _dir() / "pending"
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_pending_upload(
    data: bytes,
    *,
    original_name: str,
    label: str = "",
    color: str = "blue",
    make_default: bool = False,
    source_path: str = "",
) -> str:
    """Stash an upload while the user confirms duplicate handling."""
    import json
    import secrets
    token = secrets.token_hex(8)
    (_pending_dir() / f"{token}.bin").write_bytes(data)
    (_pending_dir() / f"{token}.json").write_text(
        json.dumps({
            "original_name": original_name,
            "label": label,
            "color": color,
            "make_default": bool(make_default),
            "source_path": source_path,
        }),
        encoding="utf-8",
    )
    return token


def load_pending_upload(token: str) -> tuple[bytes, dict[str, Any]] | None:
    import json
    token = re.sub(r"[^0-9a-f]", "", (token or "").lower())
    if len(token) < 8:
        return None
    bin_path = _pending_dir() / f"{token}.bin"
    meta_path = _pending_dir() / f"{token}.json"
    if not bin_path.is_file() or not meta_path.is_file():
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return bin_path.read_bytes(), meta


def clear_pending_upload(token: str) -> None:
    token = re.sub(r"[^0-9a-f]", "", (token or "").lower())
    if not token:
        return
    (_pending_dir() / f"{token}.bin").unlink(missing_ok=True)
    (_pending_dir() / f"{token}.json").unlink(missing_ok=True)


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


def rename_file(resume_id: int, new_name: str) -> str:
    """Rename a library resume's display name and on-disk library file.

    Keeps the original extension (HTML stays HTML, PDF stays PDF). Also renames
    ``source_path`` on disk when that file is writable. Returns the new
    ``original_name``.
    """
    row = get(resume_id)
    if not row:
        raise ValueError(f"Unknown resume id {resume_id}")

    old_orig = row["original_name"] or row["filename"] or "resume.bin"
    old_ext = Path(old_orig).suffix.lower() or Path(row["filename"] or "").suffix.lower()
    desired = _safe_name(Path(new_name or "").name)
    if not desired:
        raise ValueError("Filename cannot be empty")
    if not Path(desired).suffix and old_ext:
        desired = f"{desired}{old_ext}"
    # Lock extension so HTML/PDF type (and twin grouping) stay consistent.
    if old_ext and Path(desired).suffix.lower() != old_ext:
        desired = f"{Path(desired).stem}{old_ext}"
    new_ext = Path(desired).suffix.lower()
    if new_ext and new_ext not in SUPPORTED_RESUME_EXTS:
        raise ValueError(
            f"Unsupported resume type {new_ext} "
            f"(use {', '.join(sorted(SUPPORTED_RESUME_EXTS))})"
        )
    if desired == Path(old_orig).name:
        return desired

    content_hash = row["content_hash"] or ""
    prefix = (content_hash[:_HASH_PREFIX] or f"id{resume_id}")
    new_stored = f"{prefix}_{desired}"
    old_path = path_for(row)
    new_path = _dir() / new_stored
    if new_path.resolve() != old_path.resolve():
        if new_path.exists():
            raise ValueError(f"A library file named {new_stored} already exists")
        if old_path.is_file():
            old_path.rename(new_path)
        elif not new_path.exists():
            # Library copy missing — still update DB names so downloads/labels work.
            pass

    new_source = (row["source_path"] or "").strip()
    if new_source:
        src = Path(new_source)
        target = src.with_name(desired)
        if src.is_file() and target.resolve() != src.resolve():
            try:
                if not target.exists():
                    src.rename(target)
                    new_source = str(target)
                # If target already exists, keep pointing at the old path.
            except OSError:
                # External path not writable — leave source_path unchanged.
                pass
        elif not src.exists():
            # Recorded path is stale; update basename for display consistency.
            new_source = str(src.with_name(desired))

    with get_connection() as conn:
        conn.execute(
            """UPDATE resumes
                  SET original_name=?, filename=?, source_path=?
                WHERE id=?""",
            (desired, new_stored, new_source[:500] if new_source else "",
             resume_id),
        )

    if row["is_default"] and new_path.is_file():
        try:
            config.update_env_file({"RESUME_PATH": str(new_path.resolve())})
            config.reload()
        except OSError:
            pass
    return desired


def update_meta(resume_id: int, *, label: str | None = None,
                color: str | None = None,
                original_name: str | None = None) -> None:
    if label is not None:
        set_label(resume_id, label)
    if color is not None:
        set_color(resume_id, color)
    if original_name is not None and original_name.strip():
        rename_file(resume_id, original_name)


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
    prefer_existing: bool = True,
) -> tuple[int | None, dict[str, Any] | None]:
    """Resolve paste/detail form fields to a resume id (or None).

    Priority: new upload → new path → existing id.

    Returns ``(resume_id, info)`` where ``info`` may include::
      created, reused, match_kind, label, message
    When ``prefer_existing`` is True (default), an upload/path that matches
    existing library content (exact or similar) reuses that row instead of
    adding a near-duplicate.
    """
    info: dict[str, Any] | None = None

    if upload is not None and getattr(upload, "filename", None):
        raw = upload.read()
        if raw:
            match = match_existing(raw, original_name=upload.filename or "")
            if match and prefer_existing:
                rid = preferred_match_id(match)
                if match["kind"] == "exact":
                    msg = (
                        f"Same file content already in library as "
                        f"“{match['label']}” — using that one."
                    )
                else:
                    pct = int(round(match["score"] * 100))
                    msg = (
                        f"Very similar to “{match['label']}” ({pct}% match) — "
                        f"using the existing library resume instead of adding another copy."
                    )
                info = {
                    "created": False,
                    "reused": True,
                    "match_kind": match["kind"],
                    "label": match["label"],
                    "message": msg,
                    "resume_id": rid,
                }
                return rid, info
            rid, created = ensure_from_bytes(
                raw,
                original_name=upload.filename,
                label=upload_label,
                color=color,
            )
            row = get(rid)
            info = {
                "created": created,
                "reused": not created,
                "match_kind": "exact" if not created else None,
                "label": (row["label"] if row else "") or "",
                "message": (
                    f"Added “{row['label']}”." if created and row
                    else f"Already in library as “{row['label']}” — using that one."
                    if row else ""
                ),
                "resume_id": rid,
            }
            return rid, info

    path_text = (path_text or "").strip()
    if path_text:
        p = Path(path_text).expanduser()
        if not p.is_file():
            raise FileNotFoundError(f"Resume not found: {p}")
        raw = p.read_bytes()
        match = match_existing(raw, original_name=p.name)
        if match and prefer_existing:
            rid = preferred_match_id(match)
            if match["kind"] == "exact":
                msg = (
                    f"Same file content already in library as "
                    f"“{match['label']}” — using that one."
                )
            else:
                pct = int(round(match["score"] * 100))
                msg = (
                    f"Very similar to “{match['label']}” ({pct}% match) — "
                    f"using the existing library resume."
                )
            info = {
                "created": False,
                "reused": True,
                "match_kind": match["kind"],
                "label": match["label"],
                "message": msg,
                "resume_id": rid,
            }
            return rid, info
        rid, created = ensure_from_path(
            path_text, label=upload_label, color=color)
        row = get(rid)
        info = {
            "created": created,
            "reused": not created,
            "match_kind": "exact" if not created else None,
            "label": (row["label"] if row else "") or "",
            "message": (
                f"Added “{row['label']}”." if created and row
                else f"Already in library as “{row['label']}” — using that one."
                if row else ""
            ),
            "resume_id": rid,
        }
        return rid, info

    if resume_id in (None, "", "0", 0):
        return None, None
    rid = int(resume_id)
    row = get(rid)
    if not row:
        raise ValueError(f"Unknown resume id {rid}")
    # If a PDF twin was selected, prefer attaching the HTML primary.
    view = viewer_row(rid)
    if view and int(view["id"]) != rid:
        rid = int(view["id"])
        row = view
    info = {
        "created": False,
        "reused": True,
        "match_kind": None,
        "label": row["label"] or "",
        "message": f"Linked “{row['label']}”.",
        "resume_id": rid,
    }
    return rid, info
