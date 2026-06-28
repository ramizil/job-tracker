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


# Curated model suggestions per provider (the Settings datalists). Users can
# always type any model id; these are just convenient starting points.
OPENAI_MODELS = ["gpt-4o-mini", "gpt-4o", "gpt-4.1-mini", "gpt-4.1", "o4-mini"]
ANTHROPIC_MODELS = [
    "claude-3-5-haiku-latest", "claude-3-5-sonnet-latest",
    "claude-3-7-sonnet-latest", "claude-sonnet-4-latest",
]

_PROVIDER_LABELS = {
    "gemini": "Google Gemini",
    "openai": "OpenAI (GPT)",
    "anthropic": "Anthropic (Claude)",
}


def active_provider() -> str:
    p = (config.AI_PROVIDER or "gemini").lower()
    return p if p in _PROVIDER_LABELS else "gemini"


def provider_label(provider: str | None = None) -> str:
    return _PROVIDER_LABELS.get(provider or active_provider(), "AI")


def _provider_key(provider: str | None = None) -> str:
    p = provider or active_provider()
    return {
        "gemini": config.GEMINI_API_KEY,
        "openai": config.OPENAI_API_KEY,
        "anthropic": config.ANTHROPIC_API_KEY,
    }.get(p, "")


def is_configured() -> bool:
    """True if the currently selected AI provider has an API key set."""
    return bool(_provider_key())


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
    """Generate text using the currently selected AI provider."""
    provider = active_provider()
    if provider == "openai":
        return _generate_openai(prompt, as_json=as_json)
    if provider == "anthropic":
        return _generate_anthropic(prompt, as_json=as_json)
    return _generate_gemini(prompt, as_json=as_json, attempts=attempts)


def _generate_gemini(prompt: str, *, as_json: bool = False, attempts: int = 2) -> str:
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


def _generate_openai(prompt: str, *, as_json: bool = False) -> str:
    """Generate text with OpenAI (GPT). JSON mode uses response_format."""
    if not config.OPENAI_API_KEY:
        raise AIError("No OpenAI API key configured. Add it on the Settings page "
                      "(get one at https://platform.openai.com/api-keys).")
    try:
        from openai import OpenAI  # type: ignore
    except ImportError as exc:  # pragma: no cover
        raise AIError("openai is not installed (pip install openai).") from exc

    client = OpenAI(api_key=config.OPENAI_API_KEY, timeout=AI_TIMEOUT_S)
    model = config.OPENAI_MODEL or "gpt-4o-mini"
    base = dict(model=model, messages=[{"role": "user", "content": prompt}])
    attempts = [
        {**base, "temperature": 0.4,
         **({"response_format": {"type": "json_object"}} if as_json else {})},
        {**base, "temperature": 0.4},   # model may reject response_format
        dict(base),                      # model may reject custom temperature
    ]
    last_exc: Exception | None = None
    for kwargs in attempts:
        try:
            resp = client.chat.completions.create(**kwargs)
            text = (resp.choices[0].message.content or "").strip()
            if not text:
                raise AIError("OpenAI returned an empty response.")
            return text
        except AIError:
            raise
        except Exception as exc:
            last_exc = exc
            continue
    raise AIError(f"OpenAI request failed: {last_exc}")


def _generate_anthropic(prompt: str, *, as_json: bool = False) -> str:
    """Generate text with Anthropic (Claude)."""
    if not config.ANTHROPIC_API_KEY:
        raise AIError("No Anthropic API key configured. Add it on the Settings "
                      "page (get one at https://console.anthropic.com/).")
    try:
        import anthropic  # type: ignore
    except ImportError as exc:  # pragma: no cover
        raise AIError("anthropic is not installed (pip install anthropic).") from exc

    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY, timeout=AI_TIMEOUT_S)
    model = config.ANTHROPIC_MODEL or "claude-3-5-sonnet-latest"
    kwargs: dict[str, Any] = dict(
        model=model, max_tokens=8192, temperature=0.4,
        messages=[{"role": "user", "content": prompt}],
    )
    if as_json:
        kwargs["system"] = ("Respond with ONLY a single valid JSON object. "
                            "No prose, no explanations, no code fences.")
    try:
        msg = client.messages.create(**kwargs)
        text = "".join(
            getattr(b, "text", "") for b in msg.content
            if getattr(b, "type", None) == "text"
        ).strip()
    except Exception as exc:
        raise AIError(f"Anthropic request failed: {exc}") from exc
    if not text:
        raise AIError("Anthropic returned an empty response.")
    return text


def resume_text(resume_path: Path | None = None) -> str:
    from . import resume as _resume
    path = Path(resume_path) if resume_path else config.RESUME_PATH
    text = _resume.extract_text(path)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _text_to_resume_html(text: str) -> str:
    """Wrap plain resume text in a clean, printable HTML document.

    Used when the source resume is a PDF/Word/text file (no HTML to reuse), so
    the tailored-resume feature still has markup to work with.
    """
    import html as _html
    body = "\n".join(
        f"<p>{_html.escape(line)}</p>" if line.strip() else "<br>"
        for line in text.splitlines()
    )
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<style>body{font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;"
        "max-width:800px;margin:32px auto;line-height:1.5;color:#1f2937;}"
        "p{margin:0 0 6px;}h1,h2{color:#111827;}</style></head>"
        f"<body>{body}</body></html>"
    )


def resume_html(resume_path: Path | None = None) -> str:
    from . import resume as _resume
    path = Path(resume_path) if resume_path else config.RESUME_PATH
    if path.suffix.lower() in (".html", ".htm"):
        return path.read_text(encoding="utf-8", errors="ignore")
    return _text_to_resume_html(_resume.extract_text(path))


# --------------------------------------------------------------------------- #
_ANALYSIS_PROMPT = """You are a senior technical recruiter and career coach.
Compare the CANDIDATE RESUME against the JOB POSTING and produce a brutally
honest fit analysis. Return ONLY valid JSON with EXACTLY this shape:

LANGUAGE (bilingual, REQUIRED): Write EVERY human-readable text value (verdict,
requirement, evidence, risks, suggestion action/rationale) in BOTH English and
Hebrew, formatted as "English text — Hebrew text" (English first, then " — ",
then the Hebrew translation). For "analysis_markdown", write the full write-up
in English first, then a "## עברית" heading followed by the same write-up in
Hebrew. Keep the JSON keys, the "fit_level" value (YES/MAYBE/NO) and the "match"
value (strong/partial/gap) in English only.

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
    """Return a structured fit analysis dict (see _ANALYSIS_PROMPT).

    The analysis is always produced bilingually (English + Hebrew); the
    ``language`` argument is accepted for API compatibility but ignored.
    """
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
    data["language"] = "bi"
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
{lang_rule}

{extra}JOB:
Title: {title}
Company: {company}

CANDIDATE RESUME (plain text):
{resume}
"""


def recruiter_note(*, title: str, company: str, instructions: str = "",
                   resume: str | None = None, language: str = "en") -> str:
    """Generate a short recruiter outreach note (plain text).

    An English version is always included. If the chosen language is not
    English, the note is produced in that language first and then the English
    version below a divider; otherwise it is English only.
    """
    lang = (language or "en").lower()
    if lang == "en":
        lang_rule = "- Write the ENTIRE message in English (natural, professional English)."
    else:
        ln = _lang_name(lang)
        lang_rule = (
            f"- Produce TWO versions of the message. First the message in {ln} "
            f"(natural, professional {ln}), then a line containing only '———', "
            "then the SAME message in English. Label the first block with a line "
            f"'{ln}:' and the English block with a line 'English:'."
        )
    extra = f"ADDITIONAL INSTRUCTIONS:\n{instructions.strip()}\n\n" if instructions.strip() else ""
    prompt = _RECRUITER_NOTE_PROMPT.format(
        lang_rule=lang_rule,
        extra=extra, title=title or "", company=company or "",
        resume=(resume or resume_text())[:6000],
    )
    text = _generate(prompt, as_json=False).strip()
    text = re.sub(r"^```\w*\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text


# --------------------------------------------------------------------------- #
_FEEDBACK_REQUEST_PROMPT = """You are the candidate. Write a SHORT, gracious
email to the recruiter / hiring manager after being turned down for the job
below, politely asking for brief constructive feedback so you can improve. Rules:
- Genuinely warm and professional. NO bitterness, no arguing the decision, no
  begging to reconsider. Accept the outcome with grace.
- Thank them for their time and the opportunity to apply/interview.
- Ask for 1-2 specific, actionable pointers: what was missing, which skills or
  experience to strengthen, or how you could be a stronger fit next time.
- Optionally note you'd welcome being kept in mind for future roles.
- 5 to 9 sentences, under ~140 words. Include a subject line as the first line
  ("Subject: ..."). No markdown, no placeholders like [Name] — sign off simply.
{lang_rule}

{extra}JOB:
Title: {title}
Company: {company}
{reject_ctx}
CANDIDATE RESUME (plain text):
{resume}
"""


def feedback_request(*, title: str, company: str, stage: str = "",
                     reason: str = "", instructions: str = "",
                     resume: str | None = None, language: str = "en") -> str:
    """Generate a polite 'why was I rejected, how can I improve' email.

    Uses the resume and (when known) the logged rejection stage/reason. An
    English version is always included; a non-English language is produced first
    followed by the English version below a divider.
    """
    lang = (language or "en").lower()
    if lang == "en":
        lang_rule = "- Write the ENTIRE email in English (natural, professional English)."
    else:
        ln = _lang_name(lang)
        lang_rule = (
            f"- Produce TWO versions. First the email in {ln} (natural, "
            f"professional {ln}), then a line containing only '———', then the "
            "SAME email in English. Label the first block with a line "
            f"'{ln}:' and the English block with a line 'English:'."
        )
    ctx_bits = []
    if stage:
        ctx_bits.append(f"Rejection stage: {stage}")
    if reason:
        ctx_bits.append(f"Logged reason: {reason}")
    reject_ctx = ("\n" + "\n".join(ctx_bits) + "\n") if ctx_bits else ""
    extra = f"ADDITIONAL INSTRUCTIONS:\n{instructions.strip()}\n\n" if instructions.strip() else ""
    prompt = _FEEDBACK_REQUEST_PROMPT.format(
        lang_rule=lang_rule, extra=extra, title=title or "", company=company or "",
        reject_ctx=reject_ctx, resume=(resume or resume_text())[:6000],
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
_MOCK_INTERVIEW_PROMPT = """You are simulating a realistic, friendly job
interview for the role below. Produce a natural back-and-forth between an
INTERVIEWER and the CANDIDATE. Ground every candidate answer ONLY in the facts
in their resume — never invent employers, dates, numbers or skills.

STYLE (very important):
- Conversational and warm, NOT formal or robotic. Write the way real people
  actually speak in an interview — contractions, short sentences, a little
  personality. Avoid corporate buzzwords and clichés.
- First-person answers, spoken aloud (they will be read by a text-to-speech
  voice), so keep sentences easy to say. No bullet points, no markdown inside
  answers.
- The VERY FIRST question must be the classic opener "Tell me about yourself"
  (translated naturally if not English), and its answer is a relaxed ~45-75s
  personal pitch built from the resume.
- 7 to 9 questions total: mix the opener, a few role-specific technical ones,
  a couple of behavioural ones, and end with the candidate asking 1 good
  question back.

Write everything in {lang} (natural, native-sounding {lang}).

Return ONLY valid JSON with EXACTLY this shape:
{{
  "qa": [
    {{"q": "interviewer question", "a": "candidate's natural spoken answer"}}
  ]
}}

JOB:
Title: {title}
Company: {company}
Location: {location}
Description:
{description}

CANDIDATE RESUME (plain text):
{resume}
"""


def mock_interview(*, title: str, company: str, location: str = "",
                   description: str = "", resume: str | None = None,
                   language: str = "en") -> dict[str, Any]:
    """Generate a natural mock-interview Q&A simulation grounded in the resume.

    Returns {"language": "...", "qa": [{"q": ..., "a": ...}, ...]}.
    """
    prompt = _MOCK_INTERVIEW_PROMPT.format(
        lang=_lang_name(language),
        title=title or "", company=company or "", location=location or "",
        description=(description or "")[:7000],
        resume=(resume or resume_text())[:9000],
    )
    raw = _generate(prompt, as_json=True)
    data = _parse_json(raw)
    if isinstance(data, list):
        data = {"qa": data}
    if not isinstance(data, dict):
        raise AIError("Gemini returned an unexpected interview format. Please try again.")
    qa = data.get("qa") or []
    clean = [
        {"q": str(p.get("q", "")).strip(), "a": str(p.get("a", "")).strip()}
        for p in qa if isinstance(p, dict) and (p.get("q") or p.get("a"))
    ]
    if not clean:
        raise AIError("Gemini didn't return any interview questions. Please try again.")
    return {"language": (language or "en").lower(), "qa": clean}


# --------------------------------------------------------------------------- #
_PITCH_DRAFT_PROMPT = """You are an interview coach. Write a natural, SPOKEN
"about me" interview pitch for the candidate, grounded ONLY in the facts in
their resume — never invent employers, dates, numbers or skills. Make it easy
to memorize and say aloud.

Structure it as clear "stations" (use short headers), in this spirit:
- Opener: who I am + years of experience + main arena.
- Day-to-day engineering work: what I actually build/do.
- Innovation & impact: 1-2 standout things I built, with concrete time/effort
  saved (use real numbers only if present in the resume).
- A personal passion hook (why I love this craft).
- Closing: what I'm looking for next.
Then add a short "how to remember it" summary (one line per station).

Conversational and warm, first-person, ~250-400 words. Write everything in
{lang} (natural, native-sounding {lang}). Output plain text only, no markdown
fences.

CANDIDATE RESUME (plain text):
{resume}
"""


def pitch_from_resume(*, resume: str | None = None, language: str = "he") -> str:
    """Draft a spoken 'about me' interview pitch from the resume (plain text)."""
    prompt = _PITCH_DRAFT_PROMPT.format(
        lang=_lang_name(language),
        resume=(resume or resume_text())[:9000],
    )
    text = _generate(prompt, as_json=False).strip()
    text = re.sub(r"^```\w*\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text


_PITCH_TAILOR_PROMPT = """You are an interview coach. The candidate has a
personal "about me" pitch — a memorized spoken introduction, structured in
"stations". Adapt it for the SPECIFIC job below so it lands as well as possible,
WITHOUT inventing anything: ground every claim ONLY in the candidate's existing
pitch and resume. Keep the spoken, station-based structure and a similar length.

Return ONLY valid JSON with EXACTLY this shape:
{{
  "suggestions": [
    "short, concrete bullet on what to emphasise / add / trim for THIS job"
  ],
  "script": "the full tailored pitch, ready to memorize and say aloud"
}}

Write BOTH the suggestions and the script in {lang} (natural, native {lang}).

JOB:
Title: {title}
Company: {company}
Location: {location}
Description:
{description}

CANDIDATE'S CURRENT PITCH:
{base_pitch}

CANDIDATE RESUME (plain text):
{resume}
"""


def tailor_pitch(*, title: str, company: str, location: str = "",
                 description: str = "", base_pitch: str = "",
                 resume: str | None = None, language: str = "he") -> dict[str, Any]:
    """Tailor the about-me pitch for a specific job.

    Returns {"suggestions": [...], "script": "...", "language": "..."}.
    """
    prompt = _PITCH_TAILOR_PROMPT.format(
        lang=_lang_name(language),
        title=title or "", company=company or "", location=location or "",
        description=(description or "")[:7000],
        base_pitch=(base_pitch or "")[:6000],
        resume=(resume or resume_text())[:7000],
    )
    raw = _generate(prompt, as_json=True)
    data = _parse_json(raw)
    if not isinstance(data, dict):
        raise AIError("Gemini returned an unexpected pitch format. Please try again.")
    suggestions = data.get("suggestions") or []
    if isinstance(suggestions, str):
        suggestions = [suggestions]
    suggestions = [str(s).strip() for s in suggestions if str(s).strip()]
    script = str(data.get("script", "")).strip()
    if not script:
        raise AIError("Gemini didn't return a tailored pitch. Please try again.")
    return {"suggestions": suggestions, "script": script,
            "language": (language or "he").lower()}


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


# --------------------------------------------------------------------------- #
def _grounding_sources(resp) -> list[dict[str, str]]:
    """Extract web sources (title + uri) from Gemini grounding metadata."""
    out: list[dict[str, str]] = []
    try:
        cand = resp.candidates[0]
        gm = getattr(cand, "grounding_metadata", None)
        for ch in (getattr(gm, "grounding_chunks", None) or []):
            web = getattr(ch, "web", None)
            uri = getattr(web, "uri", None) if web else None
            if uri:
                out.append({"title": getattr(web, "title", "") or uri, "uri": uri})
    except Exception:
        pass
    seen: set[str] = set()
    uniq: list[dict[str, str]] = []
    for s in out:
        if s["uri"] not in seen:
            seen.add(s["uri"])
            uniq.append(s)
    return uniq[:8]


def _generate_grounded(prompt: str) -> tuple[str, list[dict[str, str]]]:
    """Generate text grounded in live Google Search results.

    Returns (text, sources). Raises AIError if no model could produce a
    grounded answer (the caller may then fall back to an ungrounded call).
    Web-search grounding is Gemini-only; other providers raise here so the
    caller falls back to an ungrounded answer from the selected provider.
    """
    import time

    if active_provider() != "gemini" or not config.GEMINI_API_KEY:
        raise AIError("Web-search grounding requires a Gemini API key.")

    client = _client()
    from google.genai import types  # type: ignore

    def _search_tool():
        try:
            return types.Tool(google_search=types.GoogleSearch())
        except Exception:
            try:
                return types.Tool(google_search_retrieval=types.GoogleSearchRetrieval())
            except Exception:
                return None

    tool = _search_tool()
    if tool is None:
        raise AIError("This google-genai version has no web-search tool.")

    deadline = time.monotonic() + AI_TIMEOUT_S
    last_exc: Exception | None = None
    for model in _model_candidates():
        if time.monotonic() >= deadline:
            break
        try:
            cfg = types.GenerateContentConfig(
                tools=[tool], temperature=0.3, max_output_tokens=4096)
            resp = client.models.generate_content(
                model=model, contents=prompt, config=cfg)
            text = getattr(resp, "text", None)
            if not text:
                raise AIError("Gemini returned an empty response.")
            return text, _grounding_sources(resp)
        except Exception as exc:
            last_exc = exc
            continue
    raise AIError(f"grounded generation unavailable: {last_exc}")


_COMPANY_PROMPT = """You are a job-search assistant researching a company for a
candidate who is considering applying. Using up-to-date information from the web,
write a concise, factual briefing about the company below. If you cannot verify
something, say so plainly rather than guessing. Prefer recent, reputable sources.

Use these ## sections with short bullets:
## What they do
- Industry/sector, main products or services, and who their customers are.
## Size & status
- Approx headcount, public or private, HQ and main locations, funding stage or
  stock ticker if public, and whether they appear to be growing or contracting.
## History & growth
- Founded when, key milestones, notable funding/acquisitions, and the trajectory
  over the last 1-2 years.
## Recent news
- 2-4 recent, DATED developments from roughly the last 12 months, if available.
## Angles for your application
- 3-5 specific talking points the candidate can use, plus 2-3 smart questions to
  ask the interviewer, tied to what this company actually does.

Keep it tight and skimmable. Write in {lang}.

COMPANY: {company}
{context}
"""


def company_research(*, company: str, location: str = "", title: str = "",
                     description: str = "", language: str = "en") -> str:
    """Research a company on the web and return a Markdown briefing.

    Uses Gemini with Google Search grounding when available; otherwise falls
    back to the model's own knowledge (clearly flagged) so it never hard-fails.
    """
    ctx_lines = []
    if location:
        ctx_lines.append(f"Likely location: {location}")
    if title:
        ctx_lines.append(f"Role being considered: {title}")
    if description:
        ctx_lines.append("Context from the job posting (to disambiguate the "
                         f"company):\n{description[:1500]}")
    context = "\n".join(ctx_lines)
    prompt = _COMPANY_PROMPT.format(
        lang=_lang_name(language), company=company or "", context=context)

    grounded = True
    try:
        text, sources = _generate_grounded(prompt)
    except AIError:
        grounded = False
        text, sources = _generate(prompt, as_json=False), []

    text = re.sub(r"^```\w*\s*", "", text.strip())
    text = re.sub(r"\s*```$", "", text)
    if sources:
        text += "\n\n## Sources\n" + "\n".join(
            f"- [{s['title']}]({s['uri']})" for s in sources)
    if not grounded:
        text += ("\n\n_Note: live web search wasn't available, so this is from "
                 "the model's general knowledge and may be out of date._")
    return text
