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

from . import config, question_bank


class AIError(RuntimeError):
    """Raised when the AI call cannot be completed."""


# Curated model suggestions per provider (the Settings datalists). Users can
# always type any model id; these are just convenient starting points.
OPENAI_MODELS = ["gpt-4o-mini", "gpt-4o", "gpt-4.1-mini", "gpt-4.1", "o4-mini"]
ANTHROPIC_MODELS = [
    "claude-3-5-haiku-latest", "claude-3-5-sonnet-latest",
    "claude-3-7-sonnet-latest", "claude-sonnet-4-latest",
]
# Cursor models are served through a local OpenAI-compatible proxy (see the
# Settings help text). "auto" lets Cursor pick (e.g. Composer); users can type
# any id the proxy exposes via GET /v1/models or `agent --list-models`.
CURSOR_MODELS = ["auto", "sonnet-4.5", "gpt-5.2", "gemini-3-flash", "opus-4.6"]

_PROVIDER_LABELS = {
    "gemini": "Google Gemini",
    "openai": "OpenAI (GPT)",
    "anthropic": "Anthropic (Claude)",
    "cursor": "Cursor (via local proxy)",
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
        "cursor": config.CURSOR_API_KEY,
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


def _parse_retry_delay(msg: str) -> float | None:
    """Best-effort parse of Gemini's suggested retry delay (in seconds) from a
    429 error, e.g. "retryDelay': '5s'" or "Please retry in 5.0953s."."""
    if not msg:
        return None
    m = re.search(r"retryDelay['\"]?\s*[:=]\s*['\"]?(\d+(?:\.\d+)?)\s*s", msg)
    if not m:
        m = re.search(r"retry in (\d+(?:\.\d+)?)\s*s", msg, re.IGNORECASE)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return None
    return None


_QUOTA_MSG = (
    "You've hit your Gemini quota (free-tier limit reached). Wait a minute and "
    "try again, pick a lighter model in Settings (e.g. gemini-2.5-flash or "
    "gemini-flash-lite-latest), generate fewer items at once, or enable billing "
    "to raise the limit. Details: https://ai.google.dev/gemini-api/docs/rate-limits"
)


def _generate(prompt: str, *, as_json: bool = False, attempts: int = 2) -> str:
    """Generate text using the currently selected AI provider."""
    provider = active_provider()
    if provider == "openai":
        return _generate_openai(prompt, as_json=as_json)
    if provider == "anthropic":
        return _generate_anthropic(prompt, as_json=as_json)
    if provider == "cursor":
        return _generate_cursor(prompt, as_json=as_json)
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
    quota_hit = False
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
                is_quota = "429" in msg or "RESOURCE_EXHAUSTED" in msg
                if is_quota:
                    quota_hit = True
                    # Respect Google's suggested retry delay for transient
                    # per-minute throttling; back off and retry the same model
                    # if it fits the deadline. A daily cap or a long delay
                    # falls through to the next candidate model instead.
                    delay = _parse_retry_delay(msg)
                    if (delay is not None and delay <= 45 and i < attempts - 1
                            and time.monotonic() + delay + 0.3 < deadline):
                        time.sleep(delay + 0.3)
                        continue
                    break  # try the next candidate model
                # Retry same model once on transient overload, then fall back.
                if overloaded and i < attempts - 1 and time.monotonic() < deadline:
                    time.sleep(1.2)
                    continue
                break  # move to next candidate model
    if quota_hit:
        raise AIError(_QUOTA_MSG)
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


def _generate_cursor(prompt: str, *, as_json: bool = False) -> str:
    """Generate text with Cursor models via a local OpenAI-compatible proxy.

    Cursor has no native chat/completions API, so this points the OpenAI SDK at
    a local proxy (e.g. cursor-openai-api / cursor-agent-api-proxy) that wraps
    the Cursor Agent and re-exposes an OpenAI-shaped endpoint, authenticated
    with the Cursor API key. Configure the proxy URL in Settings (CURSOR_BASE_URL).
    """
    if not config.CURSOR_API_KEY:
        raise AIError(
            "No Cursor API key configured. Add it on the Settings page "
            "(get one at https://cursor.com/dashboard/integrations).")
    base_url = (config.CURSOR_BASE_URL or "http://localhost:8080/v1").strip()
    try:
        from openai import OpenAI  # type: ignore
    except ImportError as exc:  # pragma: no cover
        raise AIError("openai is not installed (pip install openai).") from exc

    client = OpenAI(api_key=config.CURSOR_API_KEY, base_url=base_url,
                    timeout=AI_TIMEOUT_S)
    model = config.CURSOR_MODEL or "auto"
    base = dict(model=model, messages=[{"role": "user", "content": prompt}])
    # Agent-backed proxies often reject response_format / custom temperature, so
    # try the richest request first and progressively fall back to the plainest.
    attempts = [
        {**base, "temperature": 0.4,
         **({"response_format": {"type": "json_object"}} if as_json else {})},
        {**base, "temperature": 0.4},
        dict(base),
    ]
    last_exc: Exception | None = None
    for kwargs in attempts:
        try:
            resp = client.chat.completions.create(**kwargs)
            text = (resp.choices[0].message.content or "").strip()
            if not text:
                raise AIError("Cursor returned an empty response.")
            return text
        except AIError:
            raise
        except Exception as exc:
            last_exc = exc
            continue
    raise AIError(
        "Cursor request failed: "
        f"{last_exc}. Make sure your local Cursor proxy is running and reachable "
        f"at {base_url} (see the Settings help text).")


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


def transcribe_audio(audio_bytes: bytes, mime_type: str = "audio/wav",
                     language: str = "he") -> str:
    """Transcribe spoken audio to text using Gemini (multimodal).

    Gemini-only: speech-to-text isn't exposed through the OpenAI/Anthropic text
    paths here, so the caller should fall back to typing when the provider isn't
    Gemini. Returns the plain transcript (may be empty for silent audio).
    """
    if not audio_bytes:
        raise AIError("No audio was received to transcribe.")
    if active_provider() != "gemini" or not config.GEMINI_API_KEY:
        raise AIError(
            "Voice transcription needs a Gemini API key (set the AI provider to "
            "Gemini in Settings). You can type your answer instead.")

    import time

    client = _client()
    from google.genai import types  # type: ignore

    ln = _lang_name(language)
    instruction = (
        f"Transcribe this {ln} audio recording exactly as spoken. "
        f"Return ONLY the transcript text in {ln} — no commentary, no speaker "
        "labels, no quotation marks. If the audio is silent or unintelligible, "
        "return an empty string.")

    deadline = time.monotonic() + AI_TIMEOUT_S
    last_exc: Exception | None = None
    for model in _model_candidates():
        if time.monotonic() >= deadline:
            break
        try:
            resp = client.models.generate_content(
                model=model,
                contents=[
                    types.Part.from_bytes(data=audio_bytes, mime_type=mime_type),
                    instruction,
                ],
            )
            return (getattr(resp, "text", None) or "").strip()
        except Exception as exc:
            last_exc = exc
            continue
    raise AIError(f"Could not transcribe the audio (last error: {last_exc}). "
                  "Please try again or type your answer.")


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


def _find_style_twin(path: Path) -> Path | None:
    """Find a sibling .html file with (roughly) the same content as this resume.

    Many people keep their CV as both a styled HTML file and the PDF exported
    from it. When RESUME_PATH points at the PDF, using the HTML twin as the
    tailoring template preserves the original design instead of falling back
    to a plain-text wrapper.
    """
    import difflib
    from . import resume as _resume

    def _norm(t: str) -> str:
        return re.sub(r"\s+", " ", t).strip().lower()[:6000]

    try:
        target = _norm(_resume.extract_text(path))
    except Exception:
        return None
    if not target:
        return None
    best: tuple[float, Path] | None = None
    for cand in sorted(path.parent.glob("*.htm*")):
        try:
            text = _norm(_resume.extract_text(cand))
        except Exception:
            continue
        ratio = difflib.SequenceMatcher(a=target, b=text, autojunk=False).ratio()
        if ratio > 0.6 and (best is None or ratio > best[0]):
            best = (ratio, cand)
    return best[1] if best else None


def resume_html(resume_path: Path | None = None) -> str:
    from . import resume as _resume
    path = Path(resume_path) if resume_path else config.RESUME_PATH
    if path.suffix.lower() in (".html", ".htm"):
        return path.read_text(encoding="utf-8", errors="ignore")
    # PDF/Word source: prefer a styled HTML twin in the same folder so the
    # tailored resume (and its PDF export) keeps the original design.
    twin = _find_style_twin(path)
    if twin:
        return twin.read_text(encoding="utf-8", errors="ignore")
    return _text_to_resume_html(_resume.extract_text(path))


# --------------------------------------------------------------------------- #
_ANALYSIS_PROMPT = """You are a senior technical recruiter and career coach.
Compare the CANDIDATE RESUME against the JOB POSTING and produce a brutally
honest fit analysis. Return ONLY valid JSON with EXACTLY this shape:

LANGUAGE (bilingual, REQUIRED): Provide every human-readable text value TWICE,
in SEPARATE fields — English in the base field and its Hebrew translation in
the matching "*_he" field (or "he" key). NEVER mix English and Hebrew inside a
single string. Keep the JSON keys, the "fit_level" value (YES/MAYBE/NO), the
"match" value (strong/partial/gap) and the "target" value in English only.

{{
  "fit_level": "YES" | "MAYBE" | "NO",
  "verdict": "one concise sentence in English (e.g. 'Strong match but overqualified')",
  "verdict_he": "the same sentence in Hebrew",
  "fit_score": 0-100,
  "job_summary": "2-3 plain-language sentences in English summarising THE JOB itself: what the role is, the main responsibilities and the key must-have requirements (not the candidate)",
  "job_summary_he": "the same job summary in Hebrew",
  "requirements": [
     {{"area": "short area label in English", "area_he": "the same in Hebrew",
       "requirement": "what the job asks (English)", "requirement_he": "the same in Hebrew",
       "evidence": "what the resume shows (English)", "evidence_he": "the same in Hebrew",
       "match": "strong"|"partial"|"gap"}}
  ],
  "risks": [{{"en": "short risk bullet in English", "he": "the same in Hebrew"}}],
  "suggestions": [
     {{"target": "summary"|"skills"|"experience"|"title"|"general",
       "action": "concrete CV change to make (English)", "action_he": "the same in Hebrew",
       "rationale": "why it helps for THIS job (English)", "rationale_he": "the same in Hebrew"}}
  ],
  "analysis_markdown": "a readable markdown write-up similar to a recruiter's notes, with sections and emojis, in English ONLY",
  "analysis_markdown_he": "the same write-up in Hebrew ONLY"
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
    data.setdefault("job_summary", "")
    data.setdefault("job_summary_he", "")
    data.setdefault("suggestions", [])
    # Normalise risks to {"en", "he"} dicts (the model occasionally returns
    # plain strings; older stored analyses did too).
    risks = []
    for item in data.get("risks") or []:
        if isinstance(item, dict):
            risks.append({"en": str(item.get("en", "")).strip(),
                          "he": str(item.get("he", "")).strip()})
        elif str(item).strip():
            risks.append({"en": str(item).strip(), "he": ""})
    data["risks"] = risks
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
{bank}
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


def _mock_interview_bank_block(language: str) -> str:
    """Prompt block injecting the curated Hebrew QA question bank (Hebrew only)."""
    if (language or "").lower() != "he":
        return ""
    picks = question_bank.sample_interview_questions(10)
    if not picks:
        return ""
    lines = "\n".join(
        f"- {q['q']} (כיוון לתשובה: {q['a']})" for q in picks)
    return f"""
QUESTION BANK — real Israeli QA/automation interview questions (Hebrew):
Weave 3-5 of these classic questions into the interview, picking the ones most
relevant to THIS job, alongside your own role-specific questions. Keep their
original Hebrew phrasing (light touch-ups allowed). Use the answer hints only
as direction — the candidate's spoken answers must still be grounded in their
actual resume.
{lines}
"""


def mock_interview(*, title: str, company: str, location: str = "",
                   description: str = "", resume: str | None = None,
                   language: str = "en") -> dict[str, Any]:
    """Generate a natural mock-interview Q&A simulation grounded in the resume.

    Hebrew simulations also draw on the bundled bank of classic Israeli QA
    interview questions (see question_bank.py), so every run mixes real
    interviewer favourites with job-specific questions.
    Returns {"language": "...", "qa": [{"q": ..., "a": ...}, ...]}.
    """
    prompt = _MOCK_INTERVIEW_PROMPT.format(
        lang=_lang_name(language),
        bank=_mock_interview_bank_block(language),
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
_QA_EXERCISE_PROMPT = """You are a senior QA/automation interviewer. Create ONE
realistic hands-on interview exercise ("testing scenario question") tailored to
the JOB below — the kind of practical task this company would actually give in
a technical QA interview, set in THEIR domain and tech stack.

Follow EXACTLY the structure, depth and spirit of the EXAMPLE EXERCISE below
(it is the gold standard). Your exercise must include, in this order:

1. 📋 Problem definition: a short realistic system description for the
   company's domain, plus 2-4 concrete business rules (with real numbers,
   ranges and at least one timing/de-noising subtlety).
2. 🎯 The interview task: part A — test design (ask for the right black-box
   methodology + a full test-case suite: Happy Path, Negative Path, Boundary
   Value Analysis, Edge/System cases); part B — automation design (data-driven
   pseudo-code in a Playwright/Pytest style with clear layer separation).
3. The FULL model solution: the categorised test cases with expected results
   (boundary values spelled out exactly), a "golden tip" sentence the candidate
   can say to impress the interviewer, and the data-driven pseudo-code with a
   simulator/API abstraction — code identifiers in English, comments may follow
   the output language.
4. A short closing note on WHY this solution impresses interviewers.

Ground the scenario in the job description's domain; if it is too vague,
invent a plausible system for that company. Do NOT copy the example's fleet
scenario — create a NEW one. Write everything in {lang} (natural, native
{lang}); keep standard QA terms (Happy Path, BVA, etc.) in English. Output
Markdown only (## headings, bullet lists, one ```python code block), no
preamble.

EXAMPLE EXERCISE (structure + quality bar to imitate):
{example}

JOB:
Title: {title}
Company: {company}
Location: {location}
Description:
{description}
"""


def qa_exercise(*, title: str, company: str, location: str = "",
                description: str = "", language: str = "he") -> str:
    """Generate a practical QA testing-scenario exercise for this job (Markdown).

    Modeled on the bundled worked example (rule-based alerting system) so the
    output always has the full test-design + data-driven automation structure.
    """
    example = question_bank.load_exercise_example()
    prompt = _QA_EXERCISE_PROMPT.format(
        lang=_lang_name(language),
        example=example[:9000],
        title=title or "", company=company or "", location=location or "",
        description=(description or "")[:7000],
    )
    text = _generate(prompt, as_json=False).strip()
    text = re.sub(r"^```(?:markdown|md)?\s*\n", "", text)
    text = re.sub(r"\n```$", "", text)
    if not text:
        raise AIError("The AI returned an empty exercise. Please try again.")
    return text


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


_PITCH_REVISE_PROMPT = """You are an interview coach. The candidate has a
personal "about me" spoken pitch — a memorized interview script. Revise it
according to the INSTRUCTION below. Rules:

- Apply ONLY what the instruction asks for; keep everything else as close to
  the original wording as possible (the candidate has memorized it).
- Keep the same overall structure ("stations") and the SAME LANGUAGE as the
  original pitch, unless the instruction explicitly says otherwise.
- Keep it natural to say aloud, and ground every claim ONLY in the original
  pitch and the resume — invent nothing.
- Output the FULL revised pitch as plain text. No markdown fences, no
  commentary, no explanations — just the pitch itself.

INSTRUCTION:
{instruction}

CURRENT PITCH:
{base_pitch}

CANDIDATE RESUME (for grounding only):
{resume}
"""


def revise_pitch(*, base_pitch: str, instruction: str,
                 resume: str | None = None) -> str:
    """Rewrite the base pitch per a free-text instruction (returns plain text)."""
    instruction = (instruction or "").strip()
    if not instruction:
        raise AIError("Write an instruction first (what should change in the pitch?).")
    prompt = _PITCH_REVISE_PROMPT.format(
        instruction=instruction[:2000],
        base_pitch=(base_pitch or "")[:9000],
        resume=(resume or resume_text())[:7000],
    )
    text = _generate(prompt, as_json=False).strip()
    text = re.sub(r"^```\w*\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    if not text:
        raise AIError("The AI returned an empty revision. Please try again.")
    return text


_PITCH_TAILOR_PROMPT = """You are an interview coach. The candidate has a
personal "about me" pitch — a memorized spoken introduction, structured in
"stations". The candidate has already memorized it, so:

- KEEP the original pitch VERBATIM — same stations, same wording, same order.
  Do NOT rewrite, trim, or paraphrase any existing sentence.
- ADD one short, new closing station tailored to the SPECIFIC job below
  (3-6 spoken sentences): connect the candidate's strongest relevant
  experience to what this role needs, so the recruiter is convinced the
  candidate can deliver in it from day one. Make it impressive but natural
  to say aloud, and ground EVERY claim ONLY in the candidate's existing
  pitch and resume — invent nothing.

Return ONLY valid JSON with EXACTLY this shape:
{{
  "suggestions": [
    "short, concrete bullet on what to emphasise for THIS job when delivering the pitch"
  ],
  "script": "the original pitch verbatim + the new job-tailored closing station"
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
# Resume Builder: a spoken Hebrew interview that produces an English resume.

# Opening question is fixed (no AI call needed); follow-ups are AI-generated.
# The UI renders it as an "areas" multi-select picker (see the template).
RESUME_BUILDER_FIRST_QUESTION = (
    "שלום! אני אעזור לך לבנות קורות חיים מקצועיים, צעד אחר צעד. "
    "נתחיל מהבסיס — באילו תחומים עבדת עד היום? בחר/י מהרשימה את כל מה שרלוונטי "
    "(ואפשר להוסיף תחומים משלך)."
)


_INTERVIEW_BUILDER_PROMPT = """You are a warm, professional career interviewer
helping someone build a high-quality resume through a SPOKEN conversation IN
HEBREW. You ask ONE small question at a time, in natural, friendly Hebrew (casual
"אתה/את" is fine). Your goal is to make this EFFORTLESS for the person while
extracting everything needed for an excellent English resume.

TOPICS to cover over the whole conversation (adapt order to the flow):
- Full name and contact details (email, phone, city, LinkedIn/GitHub if any)
- Fields / domains they have worked in
- Work experience — gathered ROLE BY ROLE, in SMALL STEPS (see below)
- Education & professional studies — degrees, institutions, years, certifications
- Military / national service (RELEVANT IN ISRAEL — always ask): which service
  (IDF / national service / exempt), unit or role, rank, dates, and any skills,
  responsibilities or leadership gained that are relevant to a resume
- Significant milestones & achievements (with concrete impact/numbers if possible)
- Key tools & technologies (use the TOOLS step below)
- Languages they speak (and level)
- What kind of role they're looking for next (optional)

WORK EXPERIENCE — ask in steps, ONE thing per question, and be INSISTENT about
exact facts (a resume is weak without them). For EACH role gather, in order:
  1. The employer / company name and where it is (city).
  2. Their exact job title there.
  3. The EXACT period: start month+year and end month+year (or "present").
     If the answer is vague ("a few years", "recently", only a year with no
     month), politely ask AGAIN for the specific month and year. Do NOT move on
     until you have a concrete start and end.
  4. Their main responsibilities / what they actually did day to day.
  5. Key achievements with concrete numbers/impact where possible.
Then ask if there is ANOTHER previous role to add; if yes, repeat steps 1–5 for
it. Move to older roles until they say there are no more.

TOOLS step (do this ONCE, after you understand their main role/domain and the
technologies they touch): return "kind":"tools" with a curated "tools" array of
~14–20 tools/technologies/frameworks that are COMMON for THAT role in TODAY's
market — include the ones they already mentioned PLUS standard adjacent ones a
person in that role likely knows. In "question", ask them (in Hebrew) to tick
the ones they know and rate their level. The app shows checkboxes + a level
picker, so keep "question" short and don't list the tools inside the text.

GENERAL RULES:
- Ask in HEBREW only, ONE short question at a time.
- Follow up when an answer is vague, very short, or worth expanding.
- Never re-ask something already answered. Stay warm and encouraging.
- FINAL QUESTION: once all key topics (including military/national service) are
  reasonably covered, ask ONE last OPEN question in Hebrew inviting them to add
  anything we might have missed (e.g. volunteering, publications, side projects,
  extra certifications, awards, hobbies). Only AFTER they answer that final
  question, set "done" to true and return an empty question.

Return ONLY valid JSON with EXACTLY this shape:
{{
  "question": "the next question in Hebrew (empty string when done)",
  "topic": "short english tag, e.g. 'contact'|'experience'|'experience_dates'|'education'|'tools'|'milestones'|'languages'",
  "kind": "text" | "tools",
  "tools": ["only when kind is 'tools': a list of tool/technology names as plain strings"],
  "done": true | false
}}

CONVERSATION SO FAR (Q = interviewer question, A = the person's spoken answer):
{conversation}
"""


def _format_conversation(conversation: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for c in conversation or []:
        q = str((c or {}).get("q", "")).strip()
        a = str((c or {}).get("a", "")).strip()
        if q:
            lines.append(f"Q: {q}")
        if a:
            lines.append(f"A: {a}")
    return "\n".join(lines).strip() or "(no questions asked yet)"


def interview_question(conversation: list[dict[str, Any]]) -> dict[str, Any]:
    """Pick the next Hebrew interview question (or signal the interview is done).

    Returns {"question", "topic", "kind", "tools", "done"} where ``kind`` is
    "text" for a normal question or "tools" for a tools-selection step (with a
    suggested ``tools`` list the UI renders as checkboxes + a level picker).
    """
    prompt = _INTERVIEW_BUILDER_PROMPT.format(
        conversation=_format_conversation(conversation)[:12000])
    raw = _generate(prompt, as_json=True)
    data = _parse_json(raw)
    if not isinstance(data, dict):
        raise AIError("AI returned an unexpected question format. Please try again.")
    question = str(data.get("question", "")).strip()
    kind = str(data.get("kind", "text")).strip().lower() or "text"
    tools = data.get("tools") or []
    if not isinstance(tools, list):
        tools = []
    tools = [str(t).strip() for t in tools if str(t).strip()]
    if kind == "tools" and not tools:
        kind = "text"  # nothing to pick → fall back to a normal question
    done = bool(data.get("done")) or (not question and kind != "tools")
    return {"question": question, "topic": str(data.get("topic", "")).strip(),
            "kind": kind, "tools": tools, "done": done}


_RESUME_BUILD_PROMPT = """You are an expert resume writer. Below is the transcript
of a spoken interview (questions and answers IN HEBREW) with a job candidate, plus
a TEMPLATE resume in HTML. Write a complete, polished resume IN ENGLISH for this
candidate.

Hard rules:
- Output a COMPLETE, valid HTML document.
- REUSE the TEMPLATE's <style>/CSS, structure and overall visual design — produce
  the SAME look and feel, only populated with THIS candidate's content. Keep a
  similar section ordering (header/contact, summary, experience, education,
  skills, languages, etc.).
- Translate everything into natural, professional ENGLISH (the interview is in
  Hebrew). Keep proper nouns sensible (transliterate names/companies if needed).
- Use ONLY facts present in the interview. Do NOT invent employers, dates,
  numbers, titles or skills. If a detail is missing, omit it gracefully — never
  output placeholders like [Name] or "N/A".
- Write concise, achievement-oriented bullet points. Order experience newest
  first when dates are known. Always include each role's employer and exact
  dates when they were given.
- If the candidate rated tool/technology proficiency (e.g. "Java (Advanced)"),
  reflect it sensibly in the Skills section (e.g. group by level or note it).
- Return ONLY the HTML, no markdown fences, no commentary.

INTERVIEW TRANSCRIPT:
{conversation}

TEMPLATE RESUME HTML (reuse its style and layout):
{template_html}
"""


def build_resume_from_interview(conversation: list[dict[str, Any]],
                                template_html: str | None = None) -> str:
    """Generate a full English resume (HTML) from the Hebrew interview transcript,
    styled after the template resume. Raises AIError on failure."""
    convo = _format_conversation(conversation)
    if convo == "(no questions asked yet)" or not any(
            str((c or {}).get("a", "")).strip() for c in (conversation or [])):
        raise AIError("There's no interview content yet to build a resume from.")
    tpl = template_html if template_html is not None else resume_html()
    prompt = _RESUME_BUILD_PROMPT.format(
        conversation=convo[:14000], template_html=(tpl or "")[:30000])
    html = _generate(prompt, as_json=False).strip()
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

Keep it tight and skimmable. Write in English ONLY (a Hebrew version is produced
separately). Output Markdown only, no code fences.

COMPANY: {company}
{context}
"""

_COMPANY_TRANSLATE_HE = """Translate the following company research briefing into
natural, professional Hebrew. Keep the SAME Markdown structure and sections, but
use Hebrew section headings (e.g. ## מה הם עושים, ## גודל ומצב, ## היסטוריה
וגידול, ## חדשות אחרונות, ## זוויות לראיון). Do NOT add or remove facts.
Output Hebrew Markdown only, no code fences.

ENGLISH BRIEFING:
{brief}
"""


def company_research(*, company: str, location: str = "", title: str = "",
                     description: str = "", language: str = "en") -> dict[str, Any]:
    """Research a company on the web and return a bilingual Markdown briefing.

    Returns {"en", "he", "sources", "grounded"}. Uses Gemini with Google Search
    grounding for the English brief when available; Hebrew is a faithful
    translation of the same facts. ``language`` is accepted for API compatibility
    but both languages are always produced.
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
    prompt = _COMPANY_PROMPT.format(company=company or "", context=context)

    grounded = True
    sources: list[dict[str, str]] = []
    try:
        text_en, sources = _generate_grounded(prompt)
    except AIError:
        grounded = False
        text_en = _generate(prompt, as_json=False)

    text_en = re.sub(r"^```\w*\s*", "", text_en.strip())
    text_en = re.sub(r"\s*```$", "", text_en)
    if sources:
        text_en += "\n\n## Sources\n" + "\n".join(
            f"- [{s['title']}]({s['uri']})" for s in sources)
    if not grounded:
        text_en += ("\n\n_Note: live web search wasn't available, so this is from "
                    "the model's general knowledge and may be out of date._")

    # Hebrew: translate the English brief (same facts, separate column in UI).
    he_prompt = _COMPANY_TRANSLATE_HE.format(brief=text_en[:12000])
    text_he = _generate(he_prompt, as_json=False).strip()
    text_he = re.sub(r"^```\w*\s*", "", text_he)
    text_he = re.sub(r"\s*```$", "", text_he)

    return {"en": text_en, "he": text_he, "sources": sources, "grounded": grounded}


# --------------------------------------------------------------------------- #
_ATS_PROMPT = """You are an ATS (Applicant Tracking System) screening simulator
and resume-optimisation expert. Companies run resumes through ATS software that
parses them and ranks candidates by keyword match against the job description.

Extract the keywords/skills/phrases an ATS (or a recruiter filtering in one)
would realistically screen for in the JOB below — concrete technologies, tools,
methodologies, certifications, domain terms and seniority markers. Then check
EACH keyword against the CANDIDATE RESUME (count synonyms/close variants as a
match, e.g. "CI/CD" ~ "Jenkins pipelines", but flag exact-word gaps where the
exact term matters to a keyword filter).

Return ONLY valid JSON with EXACTLY this shape:
{{
  "ats_score": 0-100,
  "summary": "2-4 sentences in English: how this resume would rank in an ATS for this job and the single most impactful fix",
  "summary_he": "the same summary in Hebrew",
  "keywords": [
    {{"keyword": "the term as the ATS would look for it",
      "importance": "must" | "nice",
      "in_resume": true,
      "evidence": "short quote/phrasing from the resume that matches, or '' if missing",
      "fix": "if missing or weak: one concrete English sentence on where/how to add it truthfully; '' if fine",
      "fix_he": "the same fix in Hebrew, or ''"}}
  ],
  "tips": [{{"en": "short general ATS tip for THIS resume+job in English", "he": "the same in Hebrew"}}]
}}

Rules:
- 10-18 keywords, ordered by importance (must-haves first).
- Only suggest fixes that are truthful given the resume — never invent skills;
  a fix may say "only add if you actually have this".
- Keep "keyword", "evidence" and JSON values other than *_he in English
  (keywords may stay in the job's original language if not English).

JOB:
Title: {title}
Company: {company}
Location: {location}
Description:
{description}

CANDIDATE RESUME (plain text):
{resume}
"""


def ats_check(*, title: str, company: str, location: str = "",
              description: str = "", resume: str | None = None) -> dict[str, Any]:
    """Simulate an ATS keyword screen of the resume against this job.

    Returns {"ats_score", "summary", "summary_he", "keywords": [...],
    "tips": [...]} — keywords flag what is present vs missing so the user
    knows exactly which terms to add before applying.
    """
    prompt = _ATS_PROMPT.format(
        title=title or "", company=company or "", location=location or "",
        description=(description or "")[:7000],
        resume=(resume or resume_text())[:9000],
    )
    raw = _generate(prompt, as_json=True)
    data = _parse_json(raw)
    if not isinstance(data, dict) or not isinstance(data.get("keywords"), list):
        raise AIError("The AI returned an unexpected ATS-check format. Please try again.")

    keywords = []
    for k in data["keywords"]:
        if not isinstance(k, dict) or not str(k.get("keyword", "")).strip():
            continue
        keywords.append({
            "keyword": str(k.get("keyword", "")).strip(),
            "importance": ("must" if str(k.get("importance", "")).lower() == "must"
                           else "nice"),
            "in_resume": bool(k.get("in_resume")),
            "evidence": str(k.get("evidence", "") or "").strip(),
            "fix": str(k.get("fix", "") or "").strip(),
            "fix_he": str(k.get("fix_he", "") or "").strip(),
        })
    if not keywords:
        raise AIError("The AI didn't return any ATS keywords. Please try again.")

    tips = []
    for t in data.get("tips") or []:
        if isinstance(t, dict) and str(t.get("en", "")).strip():
            tips.append({"en": str(t["en"]).strip(),
                         "he": str(t.get("he", "") or "").strip()})

    try:
        score = max(0, min(100, int(float(str(data.get("ats_score", 0))))))
    except (TypeError, ValueError):
        score = 0
    return {
        "ats_score": score,
        "summary": str(data.get("summary", "")).strip(),
        "summary_he": str(data.get("summary_he", "")).strip(),
        "keywords": keywords,
        "tips": tips,
    }


# --------------------------------------------------------------------------- #
_SALARY_PROMPT = """You are a compensation researcher for the Israeli tech job
market. Estimate the MONTHLY GROSS salary (in ILS, ₪) that the employer below
would realistically offer for this position.

METHOD (in order of preference):
1. Look for published salary data for this exact company and role (Glassdoor,
   levels.fyi, AllJobs salary surveys, Drushim, jobinfo, news reports).
2. If none found, look for Israeli market ranges for this role, seniority and
   tech stack.
3. If still nothing concrete, ESTIMATE from what is known about the company:
   its size, headcount, funding/revenue, growth stage, industry (e.g. defense,
   enterprise, startup) and location — and say clearly that it is an estimate.

Return ONLY valid JSON with EXACTLY this shape (numbers are plain integers in
ILS per month, gross):

{{
  "range_low": 20000,
  "range_high": 28000,
  "confidence": "high" | "medium" | "low",
  "basis": "published-data" | "market-range" | "company-estimate",
  "summary": "3-6 sentences in English: how the range was derived, what factors move it up or down for THIS candidate/company, and negotiation context",
  "summary_he": "the same summary in Hebrew",
  "factors": [{{"en": "short factor bullet in English", "he": "the same in Hebrew"}}]
}}

JOB:
Title: {title}
Company: {company}
Location: {location}
Description (excerpt):
{description}
"""


def salary_research(*, title: str, company: str, location: str = "",
                    description: str = "") -> dict[str, Any]:
    """Research/estimate the expected monthly gross salary (ILS) for a job.

    Prefers live web-grounded research (Gemini). When grounding is unavailable
    (other providers / no search tool) it falls back to a model estimate based
    on the company profile, flagged via "grounded": false. Returns a dict with
    range_low/range_high (ILS gross per month), confidence, basis, bilingual
    summary and factors, plus optional web sources.
    """
    prompt = _SALARY_PROMPT.format(
        title=title or "", company=company or "", location=location or "",
        description=(description or "")[:5000],
    )
    grounded = True
    sources: list[dict[str, str]] = []
    try:
        raw, sources = _generate_grounded(prompt)
    except AIError:
        grounded = False
        raw = _generate(prompt, as_json=True)

    data = _parse_json(raw)
    if not isinstance(data, dict):
        raise AIError("The AI returned an unexpected salary format. Please try again.")

    def _num(v) -> int | None:
        try:
            n = int(float(str(v).replace(",", "").replace("₪", "").strip()))
            return n if n > 0 else None
        except (TypeError, ValueError):
            return None

    low, high = _num(data.get("range_low")), _num(data.get("range_high"))
    if low and high and low > high:
        low, high = high, low
    if not (low and high):
        raise AIError("The AI couldn't produce a salary range. Please try again.")

    factors = []
    for item in data.get("factors") or []:
        if isinstance(item, dict):
            factors.append({"en": str(item.get("en", "")).strip(),
                            "he": str(item.get("he", "")).strip()})
        elif str(item).strip():
            factors.append({"en": str(item).strip(), "he": ""})

    return {
        "range_low": low,
        "range_high": high,
        "confidence": str(data.get("confidence", "low")).lower(),
        "basis": str(data.get("basis", "company-estimate")),
        "summary": str(data.get("summary", "")).strip(),
        "summary_he": str(data.get("summary_he", "")).strip(),
        "factors": factors,
        "sources": sources,
        "grounded": grounded,
    }
