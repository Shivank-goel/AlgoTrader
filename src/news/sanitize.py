"""Headline sanitization.

Headlines are untrusted, attacker-controllable text: anyone who can get a
story syndicated (or who runs a low-tier outlet in an RSS feed) can put
arbitrary characters in front of the model. This module is the first of
several layers; it is NOT the security boundary on its own.

The real defence lives in the agent output schema — a classification result
contains no size, veto or stop field, so no successful injection can reach a
risk knob. See src/agents/schemas.py (Phase 2).
"""

from __future__ import annotations

import html
import logging
import re
import unicodedata

logger = logging.getLogger(__name__)

MAX_HEADLINE_CHARS = 200

_TAG = re.compile(r"<[^>]{0,200}>")
_WHITESPACE = re.compile(r"\s+")

# Bidi/format controls used to visually reorder text so a rendered headline
# differs from the bytes the model sees.
_BIDI_CONTROLS = {
    "‪", "‫", "‬", "‭", "‮",
    "⁦", "⁧", "⁨", "⁩",
    "‎", "‏", "؜",
}

# Patterns that look like an attempt to address the model rather than report
# news. Matches are removed and counted, not merely flagged.
_INJECTION_PATTERNS = [
    re.compile(r"ignore\s+(?:all\s+|any\s+|the\s+)?(?:previous|prior|above)\s+\w*\s*instructions?", re.I),
    re.compile(r"disregard\s+(?:all\s+|any\s+|the\s+)?(?:previous|prior|above)\b", re.I),
    re.compile(r"^\s*(?:system|assistant|user)\s*:", re.I | re.M),
    re.compile(r"</?(?:system|assistant|user|instructions?)>", re.I),
    re.compile(r"\byou\s+(?:are|must|should)\s+now\b", re.I),
    re.compile(r"\bnew\s+instructions?\b", re.I),
    re.compile(r"\boverride\s+(?:your|the)\s+\w+", re.I),
    # Long unbroken base64-ish blobs — not a headline, possibly a payload.
    re.compile(r"[A-Za-z0-9+/]{60,}={0,2}"),
]


def sanitize_headline(text: str, *, max_chars: int = MAX_HEADLINE_CHARS) -> str:
    """Reduce a raw headline to plain, bounded, instruction-free text.

    Returns "" when nothing usable survives; callers should drop the item.
    """
    if not text:
        return ""

    cleaned = html.unescape(text)
    cleaned = _TAG.sub(" ", cleaned)
    cleaned = unicodedata.normalize("NFKC", cleaned)

    cleaned = "".join(
        ch
        for ch in cleaned
        if ch not in _BIDI_CONTROLS
        and (ch == " " or unicodedata.category(ch) not in ("Cc", "Cf", "Co", "Cs"))
    )

    hits = 0
    for pattern in _INJECTION_PATTERNS:
        cleaned, n = pattern.subn(" ", cleaned)
        hits += n

    cleaned = _WHITESPACE.sub(" ", cleaned).strip()

    if hits:
        logger.warning(
            "Sanitizer stripped %d instruction-like pattern(s) from a headline: %r",
            hits,
            text[:120],
        )

    if len(cleaned) > max_chars:
        cleaned = cleaned[:max_chars].rsplit(" ", 1)[0].strip()

    return cleaned
