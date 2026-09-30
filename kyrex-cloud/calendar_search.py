"""Bounded, read-only matching for natural-language Calendar event lookups."""
from __future__ import annotations

import re
from difflib import SequenceMatcher
from datetime import timedelta

import calendar_windows

MAX_SEARCH_CHARS = 120
MAX_SEARCH_RESULTS = 100
_STOP = frozenset({
    "a", "an", "and", "appointment", "appointments", "calendar", "event",
    "events", "for", "i", "is", "it", "me", "my", "on", "the", "to",
    "what", "when", "with",
})


def normalize_query(value: str) -> str | None:
    """Return a small plain-text event query; reject empty/oversized input."""
    text = re.sub(r"\s+", " ", str(value or "")).strip(" \t\r\n?.!,;:")
    if not text or len(text) > MAX_SEARCH_CHARS or any(ord(ch) < 32 for ch in text):
        return None
    words = re.findall(r"[a-z0-9]+", text.casefold())
    useful = [word for word in words if word not in _STOP]
    if not useful or len(useful) > 12 or not any(len(word) >= 3 for word in useful):
        return None
    return text


def provider_anchor(query: str) -> str:
    """Use one informative term for Google's coarse search, then score titles."""
    words = [word for word in re.findall(r"[a-z0-9]+", query.casefold())
             if word not in _STOP]
    return max(words, key=len)


def matching_events(query: str, events: list[dict]) -> list[dict]:
    """Keep title matches with a clear majority of query terms (typo tolerant)."""
    query_words = [word for word in re.findall(r"[a-z0-9]+", query.casefold())
                   if word not in _STOP]
    matches = []
    for event in events:
        title = str(event.get("summary") or "")
        title_words = re.findall(r"[a-z0-9]+", title.casefold())
        hits = 0
        for word in query_words:
            if any(
                word == candidate
                or (len(word) >= 4 and candidate.startswith(word))
                or (len(candidate) >= 4 and word.startswith(candidate))
                or (min(len(word), len(candidate)) >= 5
                    and SequenceMatcher(None, word, candidate).ratio() >= 0.78)
                for candidate in title_words
            ):
                hits += 1
        if hits / len(query_words) >= 0.6:
            matches.append(event)
    return matches


def upcoming_window(*, now=None) -> tuple[str, str]:
    """Return local now through one year ahead, using the owner's calendar TZ."""
    start = calendar_windows.local_now(now)
    end = start + timedelta(days=365)
    return start.isoformat(), end.isoformat()
