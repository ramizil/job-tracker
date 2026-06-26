"""Natural text-to-speech via Microsoft Edge neural voices (edge-tts).

This gives genuinely natural Hebrew (and English) voices for the "My Pitch"
script, without any API key. Audio is synthesised server-side to MP3 and cached
on disk by content hash so repeat plays are instant.

The corporate network uses a TLS-intercepting proxy with a self-signed CA, which
trips edge-tts's certifi-based SSL context. We swap in a ``truststore`` context
so it trusts the OS (Windows) certificate store instead.
"""
from __future__ import annotations

import asyncio
import hashlib
import ssl

from . import config

# Curated, natural-sounding neural voices grouped by language. The first entry
# in each list is the default for that language.
VOICES: dict[str, list[dict[str, str]]] = {
    "he": [
        {"id": "he-IL-HilaNeural", "label": "Hila — Hebrew, female"},
        {"id": "he-IL-AvriNeural", "label": "Avri — Hebrew, male"},
    ],
    "en": [
        {"id": "en-US-AriaNeural", "label": "Aria — US English, female"},
        {"id": "en-US-GuyNeural", "label": "Guy — US English, male"},
        {"id": "en-GB-SoniaNeural", "label": "Sonia — UK English, female"},
        {"id": "en-GB-RyanNeural", "label": "Ryan — UK English, male"},
    ],
}

_VALID_VOICES = {v["id"] for group in VOICES.values() for v in group}
_CACHE_DIR = config.DATA_DIR / "tts_cache"


def voices_for(lang: str) -> list[dict[str, str]]:
    """Voices for a base language tag like ``he`` or ``en`` (defaults to en)."""
    base = (lang or "en").split("-")[0].lower()
    return VOICES.get(base, VOICES["en"])


def all_voices() -> dict[str, list[dict[str, str]]]:
    return VOICES


def _ssl_context() -> ssl.SSLContext:
    """An SSL context that trusts the OS cert store (handles corp proxy CAs)."""
    try:
        import truststore

        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception:
        return ssl.create_default_context()


def _rate_param(rate: float) -> str:
    """Convert a 0.5–2.0 speed multiplier to an edge-tts ``+NN%`` / ``-NN%``."""
    try:
        pct = round((float(rate) - 1.0) * 100)
    except (TypeError, ValueError):
        pct = 0
    pct = max(-50, min(100, pct))
    return f"{pct:+d}%"


def synthesize(text: str, voice: str, rate: float = 1.0) -> bytes:
    """Return MP3 audio for ``text`` in ``voice``. Cached on disk by hash."""
    text = (text or "").strip()
    if not text:
        raise ValueError("Nothing to synthesize.")
    if voice not in _VALID_VOICES:
        raise ValueError(f"Unknown voice: {voice}")

    rate_str = _rate_param(rate)
    key = hashlib.sha256(f"{voice}|{rate_str}|{text}".encode("utf-8")).hexdigest()
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cached = _CACHE_DIR / f"{key}.mp3"
    if cached.exists() and cached.stat().st_size > 0:
        return cached.read_bytes()

    audio = _run(_synth_async(text, voice, rate_str))
    if not audio:
        raise RuntimeError("The voice service returned no audio.")
    cached.write_bytes(audio)
    return audio


async def _synth_async(text: str, voice: str, rate_str: str) -> bytes:
    import edge_tts
    import edge_tts.communicate as _comm

    # Make edge-tts trust the OS cert store (corporate proxy compatibility).
    _comm._SSL_CTX = _ssl_context()

    comm = edge_tts.Communicate(text, voice, rate=rate_str)
    buf = bytearray()
    async for chunk in comm.stream():
        if chunk.get("type") == "audio" and chunk.get("data"):
            buf.extend(chunk["data"])
    return bytes(buf)


def _run(coro):
    """Run an async coroutine even if an event loop is already running."""
    try:
        return asyncio.run(coro)
    except RuntimeError:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()
