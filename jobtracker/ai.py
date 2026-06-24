"""Gemini-powered resume<->job fit analysis and resume tailoring.

Uses the google-genai SDK. All functions degrade gracefully: if no key is
configured or the API errors, they raise AIError with a readable message that
the UI surfaces to the user.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup

from . import config


class AIError(RuntimeError):
    """Raised when the AI call cannot be completed."""


def is_configured() -> bool:
    return bool(config.GEMINI_API_KEY)


def _client():
    if not config.GEMINI_API_KEY:
        raise AIError(
            "No Gemini API key configured. Add it on the Settings page "
            "(get one at https://aistudio.google.com/app/apikey)."
        )
    try:
        from google import genai  # type: ignore
    except ImportError as exc:  # pragma: no cover
        raise AIError("google-genai is not installed (pip install google-genai).") from exc
    return genai.Client(api_key=config.GEMINI_API_KEY)


# Tried in order. If the primary is overloaded (503), out of quota (429) or
# unavailable (404), we transparently fall back to the next one.
_FALLBACK_MODELS = [
    "gemini-2.5-flash", "gemini-flash-latest", "gemini-3-flash-preview",
    "gemini-2.5-pro", "gemini-2.0-flash",
]


def _model_candidates() -> list[str]:
    seen: list[str] = []
    for m in [config.GEMINI_MODEL, *_FALLBACK_MODELS]:
        if m and m not in seen:
            seen.append(m)
    return seen


def _parse_json(raw: str) -> Any:
    """Parse JSON from an LLM response, tolerating fences, prose wrappers and
    the occasional malformed/truncated output. Raises AIError if unrecoverable."""
    text = (raw or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    # 1) straight parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # 2) narrow to the outermost {...} block
    m = re.search(r"\{.*\}", text, re.DOTALL)
    candidate = m.group(0) if m else text
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass
    # 3) repair malformed / truncated LLM JSON
    try:
        from json_repair import repair_json
        fixed = repair_json(candidate, return_objects=True)
        if isinstance(fixed, (dict, list)):
            return fixed
        if isinstance(fixed, str) and fixed.strip():
            return json.loads(fixed)
    except Exception:
        pass
    raise AIError(
        "Gemini returned malformed JSON that couldn't be repaired. "
        "Please click Re-run — it usually succeeds on the next try."
    )


def _generate(prompt: str, *, as_json: bool = False, attempts: int = 2) -> str:
    import time

    client = _client()
    from google.genai import types  # type: ignore
    cfg = types.GenerateContentConfig(
        response_mime_type="application/json" if as_json else "text/plain",
        temperature=0.4,
        max_output_tokens=8192,
    )
    last_exc: Exception | None = None
    for model in _model_candidates():
        for i in range(attempts):
            try:
                resp = client.models.generate_content(
                    model=model, contents=prompt, config=cfg
                )
                text = getattr(resp, "text", None)
                if not text:
                    raise AIError("Gemini returned an empty response.")
                return text
            except Exception as exc:
                last_exc = exc
                msg = str(exc)
                overloaded = "503" in msg or "UNAVAILABLE" in msg or "overloaded" in msg
                # Retry same model once on transient overload, then fall back.
                if overloaded and i < attempts - 1:
                    time.sleep(1.2)
                    continue
                break  # move to next candidate model
    raise AIError(
        f"All Gemini models were unavailable (last error: {last_exc}). "
        "This is usually a temporary overload - try again in a moment."
    )


def resume_text(resume_path: Path | None = None) -> str:
    path = Path(resume_path) if resume_path else config.RESUME_PATH
    html = path.read_text(encoding="utf-8", errors="ignore")
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["style", "script"]):
        t.decompose()
    return re.sub(r"\n{3,}", "\n\n", soup.get_text("\n")).strip()


def resume_html(resume_path: Path | None = None) -> str:
    path = Path(resume_path) if resume_path else config.RESUME_PATH
    return path.read_text(encoding="utf-8", errors="ignore")


# --------------------------------------------------------------------------- #
_ANALYSIS_PROMPT = """You are a senior technical recruiter and career coach.
Compare the CANDIDATE RESUME against the JOB POSTING and produce a brutally
honest fit analysis. Return ONLY valid JSON with EXACTLY this shape:

{{
  "fit_level": "YES" | "MAYBE" | "NO",
  "verdict": "one concise sentence (e.g. 'Strong match but overqualified')",
  "fit_score": 0-100,
  "requirements": [
     {{"area": "string", "requirement": "what the job asks",
       "evidence": "what the resume shows", "match": "strong"|"partial"|"gap"}}
  ],
  "risks": ["short risk bullet", "..."],
  "suggestions": [
     {{"target": "summary"|"skills"|"experience"|"title"|"general",
       "action": "concrete CV change to make",
       "rationale": "why it helps for THIS job"}}
  ],
  "analysis_markdown": "a readable markdown write-up similar to a recruiter's notes, with sections and emojis"
}}

JOB POSTING:
Title: {title}
Company: {company}
Location: {location}
Description:
{description}

CANDIDATE RESUME (plain text):
{resume}
"""


def analyze_fit(*, title: str, company: str, location: str,
                description: str, resume: str | None = None) -> dict[str, Any]:
    """Return a structured fit analysis dict (see _ANALYSIS_PROMPT)."""
    prompt = _ANALYSIS_PROMPT.format(
        title=title or "", company=company or "", location=location or "",
        description=(description or "")[:8000],
        resume=(resume or resume_text())[:9000],
    )
    raw = _generate(prompt, as_json=True)
    data = _parse_json(raw)
    if not isinstance(data, dict):
        raise AIError("Gemini returned an unexpected analysis format. Please Re-run.")
    data.setdefault("fit_level", "MAYBE")
    data.setdefault("verdict", "")
    data.setdefault("suggestions", [])
    return data


# --------------------------------------------------------------------------- #
_PARSE_PROMPT = """You are a parser. Extract structured fields from the pasted
job posting below. Return ONLY valid JSON with EXACTLY this shape (use an empty
string "" for anything you cannot determine, never guess):

{{
  "title": "the job title",
  "company": "the hiring company / employer name",
  "location": "city, country (or 'Remote')",
  "salary": "salary or range if stated, else ''",
  "employment_type": "Full-time | Part-time | Contract | Intern | ''"
}}

PASTED JOB POSTING:
{text}
"""


def parse_job(text: str) -> dict[str, str]:
    """Best-effort extraction of {title, company, location, salary,
    employment_type} from a pasted job posting. Raises AIError on failure."""
    prompt = _PARSE_PROMPT.format(text=(text or "")[:8000])
    raw = _generate(prompt, as_json=True)
    data = _parse_json(raw)
    if not isinstance(data, dict):
        data = {}
    keys = ("title", "company", "location", "salary", "employment_type")
    return {k: str(data.get(k, "") or "").strip() for k in keys}


# --------------------------------------------------------------------------- #
_TAILOR_PROMPT = """You are an expert resume writer. Rewrite the candidate's
HTML resume so it is optimally positioned for the SPECIFIC job below, applying
the TAILORING INSTRUCTIONS. Hard rules:
- Output a COMPLETE, valid HTML document.
- KEEP the existing <style> / CSS and overall visual layout intact.
- Do NOT invent experience, employers, dates, or skills. Only re-emphasise,
  re-order, and re-word what is already true in the resume.
- Adjust the summary/tagline and bullet emphasis to match the job.
- Return ONLY the HTML, no markdown fences, no commentary.

JOB:
Title: {title}
Company: {company}
Description:
{description}

TAILORING INSTRUCTIONS:
{instructions}

ORIGINAL RESUME HTML:
{resume_html}
"""


def tailor_resume(*, title: str, company: str, description: str,
                  instructions: str, original_html: str | None = None) -> str:
    """Return a tailored full-HTML resume string."""
    prompt = _TAILOR_PROMPT.format(
        title=title or "", company=company or "",
        description=(description or "")[:6000],
        instructions=(instructions or "Tailor for this role.")[:4000],
        resume_html=(original_html or resume_html())[:30000],
    )
    html = _generate(prompt, as_json=False).strip()
    # Strip accidental markdown fences.
    html = re.sub(r"^```(?:html)?\s*", "", html)
    html = re.sub(r"\s*```$", "", html)
    return html
