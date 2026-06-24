"""Export applications to CSV or Excel."""
from __future__ import annotations

import csv
import io

from .tracker import list_applications

COLUMNS = [
    "id", "company", "title", "location", "status", "match_score",
    "source", "url", "salary", "contact", "resume_version",
    "date_found", "date_applied", "rejection_stage", "rejection_reason",
    "rejection_date", "ai_fit_level", "ai_verdict", "notes",
]


def to_csv() -> str:
    rows = list_applications()
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for r in rows:
        writer.writerow({c: r[c] if c in r.keys() else "" for c in COLUMNS})
    return buf.getvalue()


def to_xlsx() -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill

    rows = list_applications()
    wb = Workbook()
    ws = wb.active
    ws.title = "Applications"

    header_fill = PatternFill("solid", fgColor="1F4E79")
    header_font = Font(bold=True, color="FFFFFF")
    ws.append([c.replace("_", " ").title() for c in COLUMNS])
    for cell in ws[1]:
        cell.fill = header_fill
        cell.font = header_font

    for r in rows:
        ws.append([r[c] if c in r.keys() else "" for c in COLUMNS])

    # Reasonable column widths.
    for i, c in enumerate(COLUMNS, 1):
        width = 40 if c in ("title", "notes", "url", "ai_verdict") else 16
        ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = width
    ws.freeze_panes = "A2"

    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()
