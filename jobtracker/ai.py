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


def _lang_name(language: str | None) -> str:
    return {"he": "Hebrew", "en": "English"}.get((language or "en").lower(), "English")


# Hard ceiling for any single AI operation (seconds). Keeps a slow/overloaded
# Gemini call from hanging the request forever.
AI_TIMEOUT_S = 180


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
    # Bound each HTTP call so it cannot hang indefinitely (timeout in ms).
    try:
        from google.genai import types  # type: ignore
        return genai.Client(
            api_key=config.GEMINI_API_KEY,
            http_options=types.HttpOptions(timeout=AI_TIMEOUT_S * 1000),
        )
    except Exception:
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


# Short-lived cache so the Settings page doesn't hit the API on every load.
_MODELS_CACHE: dict[str, Any] = {"key": None, "at": 0.0, "models": []}
_MODELS_TTL_S = 300


def list_models() -> list[str]:
    """Available Gemini model ids that support text generation.

    Queries the live API (cached for a few minutes) and falls back to the
    curated list if no key is set or the call fails.
    """
    import time

    key = config.GEMINI_API_KEY or ""
    now = time.monotonic()
    if (_MODELS_CACHE["key"] == key and _MODELS_CACHE["models"]
            and now - _MODELS_CACHE["at"] < _MODELS_TTL_S):
        return list(_MODELS_CACHE["models"])

    models: list[str] = []
    if key:
        try:
            client = _client()
            _SKIP = ("embedding", "aqa", "imagen", "veo", "-tts", "gemma",
                     "image", "computer-use", "native-audio", "-live",
                     "robotics", "translate", "vision")
            for m in client.models.list():
                name = (getattr(m, "name", "") or "").split("/")[-1]
                if not name or any(s in name for s in _SKIP):
                    continue
                actions = (getattr(m, "supported_actions", None)
                           or getattr(m, "supported_generation_methods", None) or [])
                if actions and not any("generatecontent" in str(a).lower() for a in actions):
                    continue
                models.append(name)
            models = sorted(set(models))
            gem = [m for m in models if m.startswith("gemini")]
            models = gem or models
        except Exception:
            models = []

    if not models:
        models = list(_FALLBACK_MODELS)

    _MODELS_CACHE.update(key=key, at=now, models=models)
    return list(models)


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

    def _cfg_for(model: str):
        kwargs = dict(
            response_mime_type="application/json" if as_json else "text/plain",
            temperature=0.4,
            max_output_tokens=8192,
        )
        # Disable the slow "thinking" pass on flash/lite models (big latency
        # win). Pro models often require thinking, so leave them untouched.
        low = model.lower()
        if "flash" in low or "lite" in low:
            try:
                kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=0)
            except Exception:
                pass
        try:
            return types.GenerateContentConfig(**kwargs)
        except Exception:
            kwargs.pop("thinking_config", None)
            return types.GenerateContentConfig(**kwargs)

    deadline = time.monotonic() + AI_TIMEOUT_S
    last_exc: Exception | None = None
    for model in _model_candidates():
        if time.monotonic() >= deadline:
            break
        cfg = _cfg_for(model)
        for i in range(attempts):
            if time.monotonic() >= deadline:
                break
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
                if overloaded and i < attempts - 1 and time.monotonic() < deadline:
                    time.sleep(1.2)
                    continue
                break  # move to next candidate model
    if last_exc and ("timeout" in str(last_exc).lower() or time.monotonic() >= deadline):
        raise AIError(
            f"Gemini didn't respond within {AI_TIMEOUT_S // 60} minutes "
            "(it may be overloaded). Please try again."
        )
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

LANGUAGE: Write all human-readable text values (verdict, requirement, evidence,
risks, suggestion action/rationale, analysis_markdown) in {lang}. Keep the JSON
keys, the "fit_level" value (YES/MAYBE/NO) and the "match" value
(strong/partial/gap) in English regardless of language.

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
                description: str, resume: str | None = None,
                language: str = "en") -> dict[str, Any]:
    """Return a structured fit analysis dict (see _ANALYSIS_PROMPT)."""
    prompt = _ANALYSIS_PROMPT.format(
        lang=_lang_name(language),
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
    data["language"] = (language or "en").lower()
    return data


# --------------------------------------------------------------------------- #
_PARSE_PROMPT = """You are a parser. Extract structured fields from the pasted
job posting below. The posting may be in English or Hebrew and may come from
LinkedIn, AllJobs, Drushim, JobMaster, Glassdoor, Indeed or a company site,
so ignore site navigation/boilerplate and focus on the actual vacancy.

Return ONLY valid JSON with EXACTLY this shape (use an empty string "" for
anything you cannot determine, never guess):

{{
  "title": "the job title (keep its original language)",
  "company": "the hiring company / employer name",
  "location": "city, country (or 'Remote'). For Israeli cities include 'Israel'",
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
_COVER_LETTER_PROMPT = """You are an expert career writer. Write a tailored
cover letter for the candidate applying to the job below, grounded ONLY in the
facts present in their resume. Rules:
- 3 to 4 short paragraphs, ~250-350 words total.
- Confident and specific, never generic or boastful. No clichés like
  "I am writing to apply".
- Open with a strong hook tying the candidate's strongest, relevant experience
  to this specific role/company.
- Reference 2-3 concrete requirements from the job and the matching evidence.
- Do NOT invent employers, dates, titles, metrics, or skills.
- Plain text only (no markdown, no placeholders like [Your Name] unless the
  name is unknown). End with a brief, warm sign-off.
- Write the ENTIRE letter in {lang} (natural, professional {lang}).

{extra}JOB:
Title: {title}
Company: {company}
Location: {location}
Description:
{description}

CANDIDATE RESUME (plain text):
{resume}
"""


def cover_letter(*, title: str, company: str, location: str = "",
                 description: str = "", instructions: str = "",
                 resume: str | None = None, language: str = "en") -> str:
    """Generate a tailored cover letter (plain text). Raises AIError on failure."""
    extra = f"ADDITIONAL INSTRUCTIONS:\n{instructions.strip()}\n\n" if instructions.strip() else ""
    prompt = _COVER_LETTER_PROMPT.format(
        lang=_lang_name(language),
        extra=extra, title=title or "", company=company or "",
        location=location or "", description=(description or "")[:6000],
        resume=(resume or resume_text())[:9000],
    )
    text = _generate(prompt, as_json=False).strip()
    text = re.sub(r"^```\w*\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text


_RECRUITER_NOTE_PROMPT = """You are the candidate. Write a SHORT outreach
message to the recruiter / hiring manager for the job below — the kind of note
sent with a LinkedIn connection request or a job application. Rules:
- 3 to 5 sentences, under ~90 words. Warm, professional, direct.
- Mention the specific role and 1-2 of the strongest, relevant strengths.
- A light, confident call to action at the end.
- No markdown, no subject line, no placeholders. Just the message body.
- Write the ENTIRE message in {lang} (natural, professional {lang}).

{extra}JOB:
Title: {title}
Company: {company}

CANDIDATE RESUME (plain text):
{resume}
"""


def recruiter_note(*, title: str, company: str, instructions: str = "",
                   resume: str | None = None, language: str = "en") -> str:
    """Generate a short recruiter outreach note (plain text)."""
    extra = f"ADDITIONAL INSTRUCTIONS:\n{instructions.strip()}\n\n" if instructions.strip() else ""
    prompt = _RECRUITER_NOTE_PROMPT.format(
        lang=_lang_name(language),
        extra=extra, title=title or "", company=company or "",
        resume=(resume or resume_text())[:6000],
    )
    text = _generate(prompt, as_json=False).strip()
    text = re.sub(r"^```\w*\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text


# --------------------------------------------------------------------------- #
_INTERVIEW_PREP_PROMPT = """You are an experienced interview coach preparing the
candidate for an interview or take-home/technical test for the job below. Use
ONLY the facts in their resume; never invent experience. Produce a practical,
specific prep guide in Markdown with these sections (use ## headings and bullet
lists, keep it skimmable):

## Likely technical questions
- 6-10 questions an interviewer would realistically ask for THIS role, based on
  the job's required skills. For each, add a one-line hint on how the candidate
  should answer using their actual background.

## Behavioural / fit questions
- 4-6 likely behavioural questions, each with a short STAR-style angle the
  candidate can use, grounded in their real experience.

## Likely take-home / live test
- What a practical test for this role probably looks like, and 3-5 concrete tips
  to do well on it.

## Address your gaps
- For each notable gap/risk vs the job, a short, honest way to handle it if asked.

## Smart questions to ask them
- 4-6 thoughtful questions the candidate should ask the interviewer.

## Quick prep checklist
- 4-6 actionable things to review/practise before the interview.

Write everything in {lang} (natural, professional {lang}). Output Markdown only,
no preamble.

{extra}JOB:
Title: {title}
Company: {company}
Location: {location}
Description:
{description}

CANDIDATE RESUME (plain text):
{resume}
"""


def interview_prep(*, title: str, company: str, location: str = "",
                   description: str = "", instructions: str = "",
                   resume: str | None = None, language: str = "en") -> str:
    """Generate an interview / test preparation guide (Markdown)."""
    extra = f"ADDITIONAL INSTRUCTIONS:\n{instructions.strip()}\n\n" if instructions.strip() else ""
    prompt = _INTERVIEW_PREP_PROMPT.format(
        lang=_lang_name(language),
        extra=extra, title=title or "", company=company or "",
        location=location or "", description=(description or "")[:7000],
        resume=(resume or resume_text())[:9000],
    )
    text = _generate(prompt, as_json=False).strip()
    text = re.sub(r"^```\w*\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text


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
