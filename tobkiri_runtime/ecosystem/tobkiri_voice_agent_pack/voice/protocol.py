"""Bounded, provider-neutral speech directives from the voice prototype.

This module is staged as a Pack asset. The UI and transport do not invoke it yet.
"""

from __future__ import annotations

import re

_COLON = re.compile(r"(?:(?<=^)|(?<=[\s　]))(SAY|WAIT|SCHEDULE|ASK)\s*:", re.IGNORECASE)
_BARE = re.compile(r"(?<!\S)(CONTINUE|FINISH|ASK)(?=\s*$)", re.IGNORECASE | re.MULTILINE)
_DURATION = re.compile(r"\d+(?:\.\d+)?")
_MAX_DELAY = 120.0


def parse_directives(raw: str) -> list[tuple[str, str]]:
    """Parse line directives, including model output packed onto one line."""
    source = raw.replace("```", "")
    markers = [
        (match.start(), match.end(), match.group(1).lower()) for match in _COLON.finditer(source)
    ]
    markers += [
        (match.start(), match.end(), match.group(1).lower()) for match in _BARE.finditer(source)
    ]
    markers.sort()
    if not markers:
        content = source.strip()
        return [("say", content)] if content else []
    result: list[tuple[str, str]] = []
    if source[: markers[0][0]].strip():
        result.append(("say", source[: markers[0][0]].strip()))
    for index, (_, end, kind) in enumerate(markers):
        next_start = markers[index + 1][0] if index + 1 < len(markers) else len(source)
        result.append((kind, source[end:next_start].strip()))
    return result


def _seconds(raw: str) -> float:
    match = _DURATION.search(raw)
    return min(float(match.group()) if match else 0.0, _MAX_DELAY)


def speech_plan(raw: str) -> dict[str, object]:
    """Build a bounded playback plan; the first terminal directive wins."""
    segments: list[dict[str, float | str]] = []
    delay = 0.0
    scheduled = 0
    for kind, argument in parse_directives(raw):
        if kind == "wait":
            delay = min(delay + _seconds(argument), _MAX_DELAY)
        elif kind == "schedule":
            seconds, separator, message = argument.partition(":")
            if separator and message.strip() and scheduled < 6:
                segments.append({"delay": _seconds(seconds), "text": message.strip()})
                scheduled += 1
        elif kind in {"say", "ask"}:
            if argument and len(segments) < 6:
                if not (kind == "ask" and any(item["text"] == argument for item in segments)):
                    segments.append({"delay": delay, "text": argument})
                delay = 0.0
            if kind == "ask":
                return {"segments": segments, "continue": False, "finish": False}
        elif kind in {"continue", "finish"}:
            return {
                "segments": segments,
                "continue": kind == "continue",
                "finish": kind == "finish",
            }
    return {"segments": segments, "continue": False, "finish": False}
